#!/usr/bin/env python3
# ruff: noqa: E501  — long string literals in role preambles and prompt text are intentional
"""
auto_ai_audit.py — Automated DS/GAI external audit gate
(Step 4 of mandatory patch sequence) + autonomous meta-audit mode.

MODES
─────
Patch-gate mode (default):
    Submits an IDENTICAL prompt to both DeepSeek (DS) and Google Gemini (GAI),
    returning structured JSON + printing both raw responses so Claude can
    generate the 3-Point AI Summary inline.

    python3 auto_ai_audit.py --prompt "Your full audit prompt here"
    python3 auto_ai_audit.py --prompt-file /path/to/prompt.txt
    echo "Your prompt" | python3 auto_ai_audit.py

Meta-audit mode (--meta-audit):
    Auto-reads today's midday + nightly Gemini audit reports, recent trade
    events (Slack notification proxy), and recent bot log. Constructs a
    cross-review prompt and submits to both DS + Gemini independently.
    DS is the primary cross-reviewer (genuinely independent model).
    Gemini performs a self-consistency check on its own prior output.
    Posts a Slack summary of findings on completion.

    python3 auto_ai_audit.py --meta-audit
    python3 auto_ai_audit.py --meta-audit --no-slack   # suppress Slack post

OUTPUT
──────
    logs/ai_audit_YYYYMMDD_HHMMSS_PT.json       patch-gate mode
    logs/ai_audit_meta_YYYYMMDD_HHMMSS_PT.json  meta-audit mode
    (atomic write via tmp→replace in both cases)

EXIT CODES
──────────
    0 — both APIs succeeded
    1 — partial (one API failed; partial results written)
    2 — both APIs failed; no usable output

BLOCKED during RTH: 9:30 AM–4:00 PM ET weekdays.
Use --no-rth-block for testing only — never during live trading.

ENVIRONMENT VARIABLES
─────────────────────
    DEEPSEEK_API_KEY    — DeepSeek API key (sk-...)
    GEMINI_API_KEY      — Google Gemini API key
    SLACK_WEBHOOK_URL   — Slack incoming webhook (meta-audit Slack post)
    DEEPSEEK_BASE_URL   — optional override (default: https://api.deepseek.com)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Absolute path anchors (RC-2 prevention) ───────────────────────────────────
_HERE = Path(__file__).resolve().parent
_LOGS_DIR = _HERE / "logs"

# ── Timezone constants (RC-1 prevention) ─────────────────────────────────────
_ET = ZoneInfo("America/New_York")
_PT = ZoneInfo("America/Los_Angeles")

# ── API constants ─────────────────────────────────────────────────────────────
_DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
_DEEPSEEK_MODEL = "deepseek-chat"
_GEMINI_MODEL = "gemini-3.1-pro-preview"  # matches Google AI Studio selection
_API_TIMEOUT_S = 180  # 3-minute wall-clock limit per API call

# ── Meta-audit constants ──────────────────────────────────────────────────────
_BOT_LOG_TAIL_LINES = 100          # bot log lines for system health context
_TRADE_EVENTS_DAYS_BACK = 7        # load ALL events from past 7 days (not just tail)
_FILLS_DAYS_BACK = 7               # Alpaca fills window
_CHART_PROXY_BARS = 30             # daily bars for chart proxy calc per symbol
_DIRECTIVES_HISTORY_WEEKS = 4      # prior audit entries to include for compliance check
_MIN_FILLS_FOR_DIRECTIVES = 20     # N < this → observe only, NO parameter directives

# ── Adversarial role preambles (Round 2 DS/GAI finding — prevent groupthink) ─
_DS_ROLE_PREAMBLE = (
    "You are a SKEPTICAL RISK AUDITOR reviewing an Alpaca paper trading bot.\n"
    "YOUR MANDATE: Find evidence this bot should be paused or its parameters tightened.\n"
    "DEFAULT STANCE: Assume the worst interpretation of ambiguous data. "
    "Challenge every apparent win. Surface hidden fragility.\n"
    "ANALYTICAL LENSES:\n"
    "  - Nassim Taleb (Antifragile, The Black Swan): "
    "Is recent P&L luck or edge? Is this system fragile to tail events?\n"
    "  - Larry Harris (Trading and Exchanges): "
    "Is adverse selection or execution leakage consuming alpha?\n"
    "  - Thomas Peterffy (IBKR infrastructure): "
    "Where will this system fail silently under load or edge conditions?\n"
    "If you cannot find evidence of positive expectancy, say so explicitly — "
    "do not invent edge.\n\n"
)

_GAI_ROLE_PREAMBLE = (
    "You are an ALPHA OPTIMIZER reviewing an Alpaca paper trading bot.\n"
    "YOUR MANDATE: Find evidence this bot's edge is being suppressed by "
    "overly conservative parameters. Find alpha left on the table.\n"
    "DEFAULT STANCE: Assume the best interpretation of ambiguous data. "
    "Identify where caution is costing real returns.\n"
    "ANALYTICAL LENSES:\n"
    "  - Ed Thorp (Kelly Criterion, A Man for All Markets): "
    "Is sizing sub-Kelly? What is the implied Kelly fraction from win rate + avg R?\n"
    "  - Cliff Asness (AQR research, factor investing): "
    "Is the confluence signal genuine and persistent? Are we cutting winners too early?\n"
    "  - Jegadeesh + Titman (1993 momentum paper): "
    "Is momentum continuation being truncated by stops that are too tight?\n"
    "Quantify missed alpha wherever found — estimate P&L impact, don't just flag it.\n\n"
)

# ── GitHub Gist endpoint (board CCR reads from here — raw IP is allowlisted) ──
_GIST_ID = "1574ea556d06e7a1db45d00097f9c069"
_GIST_RAW_URL = (
    f"https://gist.githubusercontent.com/redstorm8705/{_GIST_ID}"
    "/raw/meta_audit_latest.json"
)


# ── RTH block ────────────────────────────────────────────────────────────────
def _check_rth_block() -> None:
    """Exit if called during Regular Trading Hours (9:30 AM–4:00 PM ET, Mon–Fri)."""
    now_et = datetime.now(_ET)
    if now_et.weekday() < 5:
        mins = now_et.hour * 60 + now_et.minute
        if (9 * 60 + 30) <= mins < (16 * 60):
            print(
                "BLOCKED: auto_ai_audit.py cannot run during RTH "
                "(9:30 AM–4:00 PM ET / 6:30 AM–1:00 PM PT weekdays). "
                "Use --no-rth-block for testing only.",
                file=sys.stderr,
            )
            sys.exit(1)


# ── Prompt resolution (patch-gate mode) ──────────────────────────────────────
def _resolve_prompt(args: argparse.Namespace) -> str:
    """Return the audit prompt from CLI arg, file, or stdin."""
    if args.prompt:
        return args.prompt.strip()
    if args.prompt_file:
        p = Path(args.prompt_file)
        if not p.exists():
            print(f"ERROR: prompt file not found: {p}", file=sys.stderr)
            sys.exit(1)
        return p.read_text(encoding="utf-8").strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    print(
        "ERROR: No prompt supplied. Use --prompt, --prompt-file, or stdin.",
        file=sys.stderr,
    )
    sys.exit(1)


# ── Meta-audit helpers ────────────────────────────────────────────────────────
def _find_latest_audit_file(glob_pattern: str) -> Path | None:
    """Find today's audit file; fall back to the most recent available."""
    today = datetime.now(_PT).strftime("%Y-%m-%d")
    # Try today's file first (pattern must contain {date} placeholder)
    today_path = _LOGS_DIR / glob_pattern.replace("{date}", today)
    if today_path.exists() and today_path.stat().st_size > 0:
        return today_path
    # Fall back: find most recent matching file
    star_pattern = glob_pattern.replace("{date}", "*")
    candidates = sorted(_LOGS_DIR.glob(star_pattern), reverse=True)
    return next((c for c in candidates if c.stat().st_size > 0), None)


def _read_tail(path: Path, n_lines: int) -> str:
    """Return the last n_lines of a file. Empty string if missing or unreadable."""
    if not path.exists():
        return ""
    try:
        lines = path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        return "\n".join(lines[-n_lines:])
    except OSError:
        return ""


# ── Meta-audit data helpers (Round 2 redesign — no Gemini report contamination) ─

def _load_week_trade_events(days_back: int = _TRADE_EVENTS_DAYS_BACK) -> list[dict]:
    """Load ALL trade events from the past N days (not just a tail slice)."""
    path = _LOGS_DIR / "trade_events.jsonl"
    if not path.exists():
        return []
    cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(days=days_back)
    events: list[dict] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ts_raw = ev.get("ts", "")
                if ts_raw:
                    try:
                        ev_ts = datetime.fromisoformat(ts_raw)
                        if ev_ts.tzinfo is None:
                            ev_ts = ev_ts.replace(tzinfo=_PT)
                        if ev_ts.astimezone(ZoneInfo("UTC")) < cutoff:
                            continue
                    except ValueError:
                        pass  # unparseable ts — include anyway
                events.append(ev)
    except OSError:
        pass
    return events


def _load_week_rejected_signals(days_back: int = _TRADE_EVENTS_DAYS_BACK) -> list[dict]:
    """Load rejected entry signals from past N days. Returns [] if file not found.
    NOTE: rejected_signals.jsonl requires a bot-side build — infrastructure gap until then.
    """
    path = _LOGS_DIR / "rejected_signals.jsonl"
    if not path.exists():
        return []
    cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(days=days_back)
    signals: list[dict] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    sig = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ts_raw = sig.get("ts", "")
                if ts_raw:
                    try:
                        sig_ts = datetime.fromisoformat(ts_raw)
                        if sig_ts.tzinfo is None:
                            sig_ts = sig_ts.replace(tzinfo=_PT)
                        if sig_ts.astimezone(ZoneInfo("UTC")) < cutoff:
                            continue
                    except ValueError:
                        pass
                signals.append(sig)
    except OSError:
        pass
    return signals


def _fetch_week_fills(days_back: int = _FILLS_DAYS_BACK) -> list[dict]:
    """Fetch FILL activities from Alpaca paper API for the past N days (authoritative P&L source)."""
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret:
        print("[auto_ai_audit] ⚠️  ALPACA keys not set — skipping fills fetch", file=sys.stderr)
        return []
    et_start = (
        datetime.now(_ET).replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=days_back)
    )
    et_end = datetime.now(_ET)
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret}
    _fmt = "%Y-%m-%dT%H:%M:%SZ"
    import requests  # type: ignore[import-untyped]
    all_fills: list[dict] = []
    after_id: str | None = None
    for _ in range(20):  # max 20 pages × 100 fills = 2000 fills
        params: dict = {
            "after": et_start.astimezone(timezone.utc).strftime(_fmt),
            "until": et_end.astimezone(timezone.utc).strftime(_fmt),
            "page_size": 100,
        }
        if after_id:
            params["after_id"] = after_id
        try:
            resp = requests.get(
                "https://paper-api.alpaca.markets/v2/account/activities/FILL",
                headers=headers,
                params=params,
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"[auto_ai_audit] ⚠️  Fills HTTP {resp.status_code}", file=sys.stderr)
                break
            page = resp.json()
            if not isinstance(page, list) or not page:
                break
            all_fills.extend(page)
            after_id = page[-1].get("id")
            if len(page) < 100:
                break
        except Exception as exc:  # noqa: BLE001
            print(f"[auto_ai_audit] ⚠️  Fills fetch error: {exc}", file=sys.stderr)
            break
    return all_fills


def _fetch_chart_proxies(symbols: list[str]) -> dict:
    """Pull 30-day daily OHLCV for traded symbols via Alpaca Data T1.
    Returns {symbol: {5d_return_pct, spy_5d_return_pct, 5d_vs_spy_pct,
    ema20_dist_pct, atr_14d, trend}}.
    SPY is always fetched for relative return comparison.
    """
    if not symbols:
        return {}
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret:
        return {}
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret}
    import requests  # type: ignore[import-untyped]
    all_syms = list(set(list(symbols) + ["SPY"]))
    bars_data: dict = {}
    for sym in all_syms:
        try:
            resp = requests.get(
                f"https://data.alpaca.markets/v2/stocks/{sym}/bars",
                headers=headers,
                params={"timeframe": "1Day", "limit": _CHART_PROXY_BARS, "adjustment": "raw"},
                timeout=15,
            )
            if resp.status_code == 200:
                bars_data[sym] = resp.json().get("bars", [])
        except Exception as exc:  # noqa: BLE001
            print(f"[auto_ai_audit] ⚠️  Chart proxy {sym} failed: {exc}", file=sys.stderr)
    spy_bars = bars_data.get("SPY", [])
    spy_5d = (
        round((spy_bars[-1]["c"] / spy_bars[-6]["c"] - 1) * 100, 2)
        if len(spy_bars) >= 6 else None
    )
    result: dict = {}
    for sym in symbols:
        bars = bars_data.get(sym, [])
        if not bars:
            result[sym] = {"status": "NO_DATA"}
            continue
        closes = [b["c"] for b in bars]
        ret5 = (
            round((closes[-1] / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else None
        )
        ema20_dist: float | None = None
        if len(closes) >= 20:
            k = 2.0 / 21.0
            ema = closes[-20]
            for c in closes[-19:]:
                ema = ema * (1 - k) + c * k
            ema20_dist = round((closes[-1] / ema - 1) * 100, 2) if ema > 0 else None
        atr: float | None = None
        if len(bars) >= 15:
            start_i = max(1, len(bars) - 14)
            trs = [
                max(
                    bars[i]["h"] - bars[i]["l"],
                    abs(bars[i]["h"] - bars[i - 1]["c"]),
                    abs(bars[i]["l"] - bars[i - 1]["c"]),
                )
                for i in range(start_i, len(bars))
            ]
            atr = round(sum(trs) / len(trs), 2) if trs else None
        trend = (
            "ABOVE_STRONG" if (ema20_dist or 0.0) > 3.0
            else "ABOVE" if (ema20_dist or 0.0) > 0.0
            else "BELOW" if (ema20_dist or 0.0) > -3.0
            else "BELOW_STRONG"
        ) if ema20_dist is not None else "UNKNOWN"
        result[sym] = {
            "5d_return_pct": ret5,
            "spy_5d_return_pct": spy_5d,
            "5d_vs_spy_pct": (
                round(ret5 - spy_5d, 2)
                if ret5 is not None and spy_5d is not None else None
            ),
            "ema20_dist_pct": ema20_dist,
            "atr_14d": atr,
            "trend": trend,
        }
    return result


def _fetch_macro_events_week(days_back: int = 7) -> list[dict]:
    """Fetch high-impact and medium-impact US macro events from FMP for past N days."""
    fmp_key = os.environ.get("FMP_API_KEY", "")
    if not fmp_key:
        return []
    today = datetime.now(_ET).date()
    from_date = today - timedelta(days=days_back)
    import requests  # type: ignore[import-untyped]
    try:
        resp = requests.get(
            "https://financialmodelingprep.com/api/v3/economic_calendar",
            params={
                "from": from_date.isoformat(),
                "to": today.isoformat(),
                "apikey": fmp_key,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[auto_ai_audit] ⚠️  FMP macro HTTP {resp.status_code}", file=sys.stderr)
            return []
        return [
            {
                "date": e.get("date", ""),
                "event": e.get("event", ""),
                "impact": e.get("impact", ""),
                "actual": e.get("actual", ""),
                "estimate": e.get("estimate", ""),
            }
            for e in resp.json()
            if e.get("country", "").upper() == "US"
            and e.get("impact", "").upper() in ("HIGH", "MEDIUM")
        ]
    except Exception as exc:  # noqa: BLE001
        print(f"[auto_ai_audit] ⚠️  FMP macro calendar failed: {exc}", file=sys.stderr)
        return []


def _compute_trade_stats(events: list[dict], fills: list[dict]) -> dict:
    """Aggregate trade statistics: per-symbol, score dist, MRI dist, infrastructure gap detection."""
    entries = [e for e in events if e.get("event") == "entry"]
    exits = [e for e in events if e.get("event") == "exit"]
    stop_hits = [e for e in events if e.get("event") == "stop_hit"]
    partials = [e for e in events if e.get("event") == "partial_exit"]
    sym_stats: dict = {}
    for ev in entries + exits + stop_hits + partials:
        sym = ev.get("symbol", "UNKNOWN")
        if sym not in sym_stats:
            sym_stats[sym] = {"entries": 0, "exits": 0, "stop_hits": 0, "partials": 0}
        if ev.get("event") == "entry":
            sym_stats[sym]["entries"] += 1
        elif ev.get("event") == "exit":
            sym_stats[sym]["exits"] += 1
        elif ev.get("event") == "stop_hit":
            sym_stats[sym]["stop_hits"] += 1
        elif ev.get("event") == "partial_exit":
            sym_stats[sym]["partials"] += 1
    score_dist: dict = {}
    mri_dist: dict = {}
    for ev in entries:
        s = str(ev.get("score", "?"))
        score_dist[s] = score_dist.get(s, 0) + 1
        m = str(ev.get("mri_level", "?"))
        mri_dist[m] = mri_dist.get(m, 0) + 1
    # ── Infrastructure gap detection ──────────────────────────────────────
    has_components = any("components" in e for e in entries)
    has_spy_bar = any("spy_bar_pct" in e for e in entries)
    gaps: list[str] = []
    if not has_components:
        gaps.append(
            "confluence_component_breakdown — 'components' key absent from trade_events.jsonl "
            "(bot-side build required before per-component analysis is possible)"
        )
    if not has_spy_bar:
        gaps.append(
            "spy_bar_magnitude_at_entry — 'spy_bar_pct' key absent from trade_events.jsonl "
            "(board vote required; needed to distinguish strong-bar vs weak-bar entries)"
        )
    return {
        "n_entries": len(entries),
        "n_exits": len(exits),
        "n_stop_hits": len(stop_hits),
        "n_partials": len(partials),
        "n_fills": len(fills),
        "symbols_traded": sorted(sym_stats.keys()),
        "per_symbol": sym_stats,
        "score_distribution": score_dist,
        "mri_distribution": mri_dist,
        "has_component_data": has_components,
        "has_spy_bar_data": has_spy_bar,
        "infrastructure_gaps": gaps,
    }


def _load_prior_directives(n: int = _DIRECTIVES_HISTORY_WEEKS) -> list[dict]:
    """Load last N weeks of audit directives for compliance tracking."""
    path = _LOGS_DIR / "audit_directives.jsonl"
    if not path.exists():
        return []
    entries: list[dict] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return entries[-n:] if entries else []


def _append_directives_log(
    week: str, ds_text: str | None, gai_text: str | None
) -> None:
    """Append this week's audit output to audit_directives.jsonl (atomic write, RC-5 compliant)."""
    path = _LOGS_DIR / "audit_directives.jsonl"
    entry = {
        "week": week,
        "ts_pt": datetime.now(_PT).strftime("%Y-%m-%d %I:%M %p PT"),
        "ds_directives_preview": (ds_text or "")[:2000],
        "gai_directives_preview": (gai_text or "")[:2000],
        "status": "pending_review",
    }
    try:
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        existing: list[dict] = []
        if path.exists():
            with path.open(encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if raw:
                        try:
                            existing.append(json.loads(raw))
                        except json.JSONDecodeError:
                            pass
        existing.append(entry)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for e in existing:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        tmp.replace(path)
        print(f"[auto_ai_audit] 📋 Directives log updated ({len(existing)} total entries): {path.name}")
    except Exception as exc:  # noqa: BLE001
        print(f"[auto_ai_audit] ⚠️  Directives log write failed: {exc}", file=sys.stderr)


def _build_meta_audit_data_context() -> tuple[dict, dict]:
    """Build complete data context for meta-audit.
    GEMINI REPORTS INTENTIONALLY EXCLUDED (Round 2 finding: contamination — DS/Gemini
    anchored to Gemini's prior conclusions, including prior errors).
    Returns (context_dict, sources_dict).
    """
    sources: dict = {}
    print("[auto_ai_audit] 📊 Loading week trade events ...")
    events = _load_week_trade_events()
    sources["trade_events"] = f"{len(events)} events (past {_TRADE_EVENTS_DAYS_BACK}d)"
    print("[auto_ai_audit] 📊 Fetching Alpaca fills ...")
    fills = _fetch_week_fills()
    sources["fills"] = f"{len(fills)} fills (past {_FILLS_DAYS_BACK}d)"
    traded_syms = sorted({e.get("symbol", "") for e in events if e.get("symbol")})
    chart_proxies: dict = {}
    if traded_syms:
        print(f"[auto_ai_audit] 📊 Fetching chart proxies: {traded_syms} ...")
        chart_proxies = _fetch_chart_proxies(traded_syms)
        sources["chart_proxies"] = f"{len(chart_proxies)} symbols"
    else:
        sources["chart_proxies"] = "no trades this week — skipped"
    print("[auto_ai_audit] 📊 Fetching FMP macro calendar ...")
    macro_events = _fetch_macro_events_week()
    sources["macro_calendar"] = f"{len(macro_events)} US macro events past 7d"
    prior_directives = _load_prior_directives()
    sources["prior_directives"] = f"{len(prior_directives)} prior audit entries"
    bot_tail = _read_tail(_LOGS_DIR / "mtf_bot.log", _BOT_LOG_TAIL_LINES)
    sources["bot_log"] = "✅ loaded" if bot_tail else "⚠️  NOT FOUND"
    stats = _compute_trade_stats(events, fills)
    rejected_path = _LOGS_DIR / "rejected_signals.jsonl"
    if rejected_path.exists():
        rejected = _load_week_rejected_signals()
        sources["rejected_signals"] = f"{len(rejected)} rejected signals past 7d"
    else:
        rejected = []
        sources["rejected_signals"] = "⚠️  INFRASTRUCTURE_GAP — rejected_signals.jsonl not yet built"
        stats["infrastructure_gaps"].append(
            "blocked_entry_visibility — rejected_signals.jsonl not yet implemented in bot; "
            "audit cannot evaluate gate efficacy (Type II error blindness)"
        )
    return {
        "events": events,
        "fills": fills,
        "chart_proxies": chart_proxies,
        "macro_events": macro_events,
        "prior_directives": prior_directives,
        "bot_log_tail": bot_tail,
        "stats": stats,
        "rejected_signals": rejected,
    }, sources


def _format_meta_audit_body(ctx: dict) -> str:
    """Format shared data sections used by both DS and Gemini prompts."""
    stats = ctx["stats"]
    n_fills = stats["n_fills"]
    n_entries = stats["n_entries"]
    parts: list[str] = []

    # ── Bot context ───────────────────────────────────────────────────────
    parts += [
        "=== BOT CONTEXT ===",
        "alpaca-mtf-bot: 12-point MTF confluence scoring on Alpaca paper account (~$2,800 equity).",
        "Entry gate: SPY 5-min bar-over-bar (sole gate). MRI adjusts size floor + MIN_SCORE.",
        "Params: MIN_SCORE=10/12 | KELLY_FRACTION=0.25 | MAX_RISK=4%/trade | PDT max 3 day trades.",
        "",
    ]

    # ── Statistical guardrail ─────────────────────────────────────────────
    if n_fills < _MIN_FILLS_FOR_DIRECTIVES:
        guardrail = (
            f"⚠️  STATISTICAL GUARDRAIL ACTIVE: Only {n_fills} fills this week "
            f"(minimum {_MIN_FILLS_FOR_DIRECTIVES} required for parameter directives). "
            f"You MAY identify patterns and observations. You MUST NOT recommend parameter "
            f"changes (MIN_SCORE, stop size, targets, sizing). "
            f"Label any such observations as [INSUFFICIENT_SAMPLE — N={n_fills}]."
        )
    else:
        guardrail = (
            f"Sample: {n_fills} fills, {n_entries} entries — sufficient for directives."
        )
    parts += ["=== STATISTICAL GUARDRAIL ===", guardrail, ""]

    # ── Infrastructure gaps ───────────────────────────────────────────────
    gaps = stats.get("infrastructure_gaps", [])
    if gaps:
        parts += ["=== INFRASTRUCTURE GAPS (data not yet available — note but do not penalize) ==="]
        for g in gaps:
            parts.append(f"  ⚠️  {g}")
        parts.append("")

    # ── Prior directives (compliance tracking) ────────────────────────────
    prior = ctx["prior_directives"]
    if prior:
        parts += [f"=== PRIOR AUDIT DIRECTIVES (last {len(prior)} weeks — evaluate compliance) ==="]
        for d in prior:
            parts += [
                f"Week {d.get('week', '?')} | {d.get('ts_pt', '')} | Status: {d.get('status', '?')}",
                f"  DS directives: {d.get('ds_directives_preview', 'N/A')[:600]}",
                f"  GAI directives: {d.get('gai_directives_preview', 'N/A')[:600]}",
                "",
            ]
    else:
        parts += ["=== PRIOR DIRECTIVES: None (first audit run — skip compliance section) ===", ""]

    # ── Trade events with inline chart proxies ────────────────────────────
    events = ctx["events"]
    chart_proxies = ctx["chart_proxies"]
    if events:
        parts += [f"=== TRADE EVENTS — PAST {_TRADE_EVENTS_DAYS_BACK} DAYS ({len(events)} events) ==="]
        for ev in events:
            sym = ev.get("symbol", "?")
            evt = ev.get("event", "?")
            ts = ev.get("ts", "?")
            line = f"[{ts}] {evt.upper()} {sym}"
            if ev.get("score") is not None:
                line += f" | score={ev['score']}"
            if ev.get("mri_level"):
                line += f" | mri={ev['mri_level']}"
            if ev.get("price") is not None:
                line += f" | price=${ev['price']:.2f}"
            if ev.get("size") is not None:
                line += f" | qty={ev['size']}"
            if ev.get("pdt_used") is not None:
                line += f" | pdt={ev['pdt_used']}"
            parts.append(line)
            # Inline chart proxy for entry events
            if evt == "entry" and sym in chart_proxies:
                cp = chart_proxies[sym]
                if cp.get("status") != "NO_DATA":
                    parts.append(
                        f"    [chart] 5d={cp.get('5d_return_pct','?')}% "
                        f"vs_SPY={cp.get('5d_vs_spy_pct','?')}% "
                        f"ema20_dist={cp.get('ema20_dist_pct','?')}% "
                        f"trend={cp.get('trend','?')} "
                        f"atr14d={cp.get('atr_14d','?')}"
                    )
        parts.append("")
    else:
        parts += [f"=== TRADE EVENTS: None in past {_TRADE_EVENTS_DAYS_BACK} days ===", ""]

    # ── Fills summary (Alpaca-authoritative) ─────────────────────────────
    fills = ctx["fills"]
    if fills:
        parts += [f"=== ALPACA FILLS — PAST {_FILLS_DAYS_BACK} DAYS ({len(fills)} fills) ==="]
        for f in fills[:60]:
            ts = str(f.get("transaction_time", "?"))[:16]
            parts.append(
                f"  {ts} | {f.get('symbol','?')} {f.get('side','?')} "
                f"{f.get('qty','?')}sh @ ${f.get('price','?')}"
            )
        if len(fills) > 60:
            parts.append(f"  ... and {len(fills) - 60} more fills")
        parts.append("")
    else:
        parts += [f"=== FILLS: None in past {_FILLS_DAYS_BACK} days ===", ""]

    # ── Per-symbol summary ────────────────────────────────────────────────
    per_sym = stats["per_symbol"]
    if per_sym:
        parts += ["=== PER-SYMBOL SUMMARY (multi-week pattern detection) ==="]
        for sym, s in sorted(per_sym.items()):
            cp = chart_proxies.get(sym, {})
            line = (
                f"  {sym}: entries={s['entries']} exits={s['exits']} "
                f"stops={s['stop_hits']} partials={s['partials']}"
            )
            if cp and cp.get("status") != "NO_DATA":
                line += (
                    f" | 5d={cp.get('5d_return_pct','?')}% "
                    f"vs_SPY={cp.get('5d_vs_spy_pct','?')}% "
                    f"trend={cp.get('trend','?')}"
                )
            parts.append(line)
        parts.append("")

    # ── Score + MRI distributions ─────────────────────────────────────────
    if stats["score_distribution"]:
        parts += ["=== SCORE DISTRIBUTION AT ENTRY ===", "  " + str(stats["score_distribution"]), ""]
    if stats["mri_distribution"]:
        parts += ["=== MRI LEVEL AT ENTRY ===", "  " + str(stats["mri_distribution"]), ""]

    # ── Rejected signals (if infrastructure exists) ───────────────────────
    rejected = ctx["rejected_signals"]
    if rejected:
        parts += [
            f"=== REJECTED SIGNALS — PAST {_TRADE_EVENTS_DAYS_BACK} DAYS "
            f"({len(rejected)} blocked entries) ==="
        ]
        for sig in rejected[:30]:
            parts.append(
                f"  [{sig.get('ts','?')}] {sig.get('symbol','?')} "
                f"score={sig.get('score','?')} | BLOCKED: {sig.get('reason','?')}"
            )
        if len(rejected) > 30:
            parts.append(f"  ... and {len(rejected) - 30} more")
        parts.append("")

    # ── Macro calendar ────────────────────────────────────────────────────
    macro = ctx["macro_events"]
    if macro:
        parts += ["=== US MACRO EVENTS — PAST 7 DAYS (correlation with trade outcomes) ==="]
        for ev in macro:
            parts.append(
                f"  {str(ev.get('date','?'))[:10]} | {ev.get('impact','?'):6} | "
                f"{ev.get('event','?')} | actual={ev.get('actual','?')} est={ev.get('estimate','?')}"
            )
        parts.append("")
    else:
        parts += ["=== MACRO CALENDAR: No high-impact US events (or FMP_API_KEY not set) ===", ""]

    # ── Bot log tail (system health only) ────────────────────────────────
    bot_tail = ctx.get("bot_log_tail", "")
    if bot_tail:
        parts += [
            f"=== BOT LOG TAIL (last {_BOT_LOG_TAIL_LINES} lines — system health context) ===",
            bot_tail,
            "",
        ]

    # ── Requested output format ───────────────────────────────────────────
    directive_instruction = (
        "MAX 3 DIRECTIVES. Each MUST cite specific trade evidence "
        "(format: date/symbol/outcome). "
        "Format: [DIRECTIVE-N] Action | Evidence: date/symbol/outcome | Expected impact"
        if n_fills >= _MIN_FILLS_FOR_DIRECTIVES
        else (
            f"DIRECTIVES BLOCKED — insufficient sample (N={n_fills} < {_MIN_FILLS_FOR_DIRECTIVES}). "
            "You may note observations but MUST NOT prescribe parameter changes."
        )
    )
    parts += [
        "=== REQUESTED OUTPUT FORMAT ===",
        "1. TRADE-BY-TRADE VERDICT",
        "   For each ENTRY event: [symbol direction score MRI] → outcome if visible →",
        "   VERDICT: edge_confirmed | entry_error | regime_error | gtc_forced | unknown",
        "   Use chart_proxies for context: was trade with/against trend? before/after macro?",
        "",
        "2. PATTERN ANALYSIS",
        "   Top 2-3 patterns (positive OR negative). Each pattern MUST cite ≥2 specific trades.",
        "",
        "3. PRIOR DIRECTIVES COMPLIANCE",
        "   For each directive above: CONFIRMED_IMPLEMENTED | NOT_IMPLEMENTED | CANNOT_VERIFY",
        "   (Skip this section if no prior directives exist.)",
        "",
        "4. WEEKLY DIRECTIVES",
        f"   {directive_instruction}",
        "",
        "5. FINAL VERDICT",
        "   PASS / WARN / FAIL — one sentence rationale.",
        "",
    ]
    return "\n".join(parts)


def _build_ds_prompt(ctx: dict) -> str:
    """Build DeepSeek prompt: skeptical risk auditor role + shared data context."""
    return _DS_ROLE_PREAMBLE + _format_meta_audit_body(ctx)


def _build_gai_prompt(ctx: dict) -> str:
    """Build Gemini prompt: alpha optimizer role + shared data context."""
    return _GAI_ROLE_PREAMBLE + _format_meta_audit_body(ctx)


# ── Slack post (meta-audit results) ──────────────────────────────────────────
def _post_slack_summary(
    ds_result: dict,
    gai_result: dict,
    out_path: Path,
    mode_label: str = "meta-audit",
) -> None:
    """Post audit verdict summary to Slack via incoming webhook."""
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        print(
            "[auto_ai_audit] No SLACK_WEBHOOK_URL — skipping Slack post",
            file=sys.stderr,
        )
        return

    import requests  # type: ignore[import-untyped]

    ds_ok = ds_result["error"] is None
    gai_ok = gai_result["error"] is None
    now_pt = datetime.now(_PT)
    ts = now_pt.strftime("%Y-%m-%d %I:%M %p PT")

    # Build short excerpts (first 350 chars of each response)
    def _excerpt(result: dict, label: str) -> str:
        if not result["text"]:
            return f"*{label}:* ❌ FAILED — {result['error']}"
        preview = result["text"][:350].replace("\n", " ").strip()
        return f"*{label} (preview):* {preview}…"

    text = (
        f":robot_face: *Auto AI {mode_label.title()} — {ts}*\n"
        f"DS: {'✅' if ds_ok else '❌'}  |  "
        f"GAI: {'✅' if gai_ok else '❌'}\n\n"
        f"{_excerpt(ds_result, 'DeepSeek')}\n\n"
        f"{_excerpt(gai_result, 'Gemini')}\n\n"
        f"Full report: `{out_path.name}`"
    )

    try:
        resp = requests.post(
            webhook,
            json={"text": text},
            timeout=10,
        )
        resp.raise_for_status()
        print("[auto_ai_audit] ✅ Slack summary posted")
    except Exception as exc:  # noqa: BLE001
        print(
            f"[auto_ai_audit] ⚠️  Slack post failed: {exc}",
            file=sys.stderr,
        )


# ── DeepSeek call (OpenAI-compatible REST, no SDK required) ──────────────────
def _call_deepseek(prompt: str) -> dict:
    """Submit prompt to DeepSeek API.

    Returns {text, model, tokens, elapsed_s, error}.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return {
            "text": None,
            "model": _DEEPSEEK_MODEL,
            "tokens": None,
            "elapsed_s": 0,
            "error": "DEEPSEEK_API_KEY not set in environment",
        }

    import requests  # type: ignore[import-untyped]  # always available

    t0 = time.monotonic()
    try:
        resp = requests.post(
            f"{_DEEPSEEK_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": _DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            },
            timeout=_API_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return {
            "text": text,
            "model": _DEEPSEEK_MODEL,
            "tokens": usage.get("total_tokens"),
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "text": None,
            "model": _DEEPSEEK_MODEL,
            "tokens": None,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": str(exc),
        }


# ── Gemini call (google.genai SDK — replaces deprecated google.generativeai) ──
def _call_gemini(prompt: str) -> dict:
    """Submit prompt to Google Gemini API via google.genai SDK.

    Uses gemini-3.1-pro-preview to match the model selected in Google AI
    Studio. google.generativeai is deprecated; google.genai is the current SDK.
    Falls back to REST if the SDK is unavailable.

    Returns {text, model, tokens, elapsed_s, error}.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {
            "text": None,
            "model": _GEMINI_MODEL,
            "tokens": None,
            "elapsed_s": 0,
            "error": "GEMINI_API_KEY not set in environment",
        }

    t0 = time.monotonic()
    try:
        from google import genai  # type: ignore[import-untyped]
        from google.genai import types  # type: ignore[import-untyped]

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.1),
        )
        text = response.text if hasattr(response, "text") else str(response)
        usage = getattr(response, "usage_metadata", None)
        tokens = (
            getattr(usage, "total_token_count", None) if usage else None
        )
        return {
            "text": text,
            "model": _GEMINI_MODEL,
            "tokens": tokens,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": None,
        }
    except ImportError:
        return _call_gemini_rest(prompt, api_key, t0)
    except Exception as exc:  # noqa: BLE001
        return {
            "text": None,
            "model": _GEMINI_MODEL,
            "tokens": None,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": str(exc),
        }


def _call_gemini_rest(prompt: str, api_key: str, t0: float) -> dict:
    """Gemini via REST — fallback if google.genai library is unavailable."""
    import requests  # type: ignore[import-untyped]

    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{_GEMINI_MODEL}:generateContent?key={api_key}"
        )
        resp = requests.post(
            url,
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1},
            },
            timeout=_API_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return {
            "text": text,
            "model": f"{_GEMINI_MODEL}-rest",
            "tokens": None,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "text": None,
            "model": f"{_GEMINI_MODEL}-rest",
            "tokens": None,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": str(exc),
        }


# ── Atomic write (RC-5 compliance) ───────────────────────────────────────────
def _push_to_gist(data: dict) -> None:
    """Push meta_audit_latest.json to GitHub Gist so board CCR can fetch it."""
    import urllib.request  # stdlib only — no requests dependency here
    token = os.environ.get("GITHUB_GIST_TOKEN", "")
    if not token:
        print(
            "[auto_ai_audit] ⚠️  GITHUB_GIST_TOKEN not set — skipping Gist push",
            file=sys.stderr,
        )
        return
    payload = json.dumps({
        "files": {
            "meta_audit_latest.json": {
                "content": json.dumps(data, indent=2, ensure_ascii=False),
            }
        }
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/gists/{_GIST_ID}",
        data=payload,
        method="PATCH",
    )
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                print(f"[auto_ai_audit] 📤 Gist updated: {_GIST_RAW_URL}")
            else:
                print(
                    f"[auto_ai_audit] ⚠️  Gist push returned HTTP {resp.status}",
                    file=sys.stderr,
                )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[auto_ai_audit] ⚠️  Gist push failed: {exc}",
            file=sys.stderr,
        )


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to path atomically via tmp→replace (no partial writes on crash)."""
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(path)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


# ── Shared API submission + output logic ──────────────────────────────────────
def _run_audit(
    prompt: str,
    out_path: Path,
    mode_label: str,
    post_slack: bool = False,
    ds_prompt: str | None = None,
    gai_prompt: str | None = None,
) -> tuple[dict, dict]:
    """Submit prompt to DS + Gemini, write JSON, print responses.

    ds_prompt / gai_prompt: adversarial-mode overrides (meta-audit).
    When provided, DS gets ds_prompt and Gemini gets gai_prompt (different roles).
    When None, both get the shared `prompt` (patch-gate mode).

    Returns (ds_result, gai_result).
    """
    now_pt = datetime.now(_PT)
    ts_display = now_pt.strftime("%Y-%m-%d %I:%M %p PT")

    print(f"[auto_ai_audit] [{mode_label}] — {ts_display}")
    if ds_prompt is not None or gai_prompt is not None:
        print(
            f"[auto_ai_audit] Adversarial mode: "
            f"DS={len(ds_prompt or ''):,} chars (skeptic) | "
            f"GAI={len(gai_prompt or ''):,} chars (optimizer)"
        )
    else:
        print(f"[auto_ai_audit] Prompt: {len(prompt):,} chars")

    # ── DeepSeek (uses ds_prompt override in adversarial mode) ───────────
    _ds_call_prompt = ds_prompt if ds_prompt is not None else prompt
    print("[auto_ai_audit] ⏳ Calling DeepSeek ...")
    ds_result = _call_deepseek(_ds_call_prompt)
    if ds_result["error"]:
        print(
            f"[auto_ai_audit] ⚠️  DeepSeek FAILED "
            f"({ds_result['elapsed_s']}s): {ds_result['error']}",
            file=sys.stderr,
        )
    else:
        print(
            f"[auto_ai_audit] ✅ DeepSeek OK "
            f"({ds_result['elapsed_s']}s, {ds_result['tokens']} tokens)"
        )

    # ── Gemini (uses gai_prompt override in adversarial mode) ────────────
    _gai_call_prompt = gai_prompt if gai_prompt is not None else prompt
    print("[auto_ai_audit] ⏳ Calling Gemini ...")
    gai_result = _call_gemini(_gai_call_prompt)
    if gai_result["error"]:
        print(
            f"[auto_ai_audit] ⚠️  Gemini FAILED "
            f"({gai_result['elapsed_s']}s): {gai_result['error']}",
            file=sys.stderr,
        )
    else:
        print(
            f"[auto_ai_audit] ✅ Gemini OK "
            f"({gai_result['elapsed_s']}s, {gai_result['tokens']} tokens)"
        )

    # ── Build output dict ─────────────────────────────────────────────────
    ds_ok = ds_result["error"] is None
    gai_ok = gai_result["error"] is None

    _effective_prompt = ds_prompt or prompt
    output = {
        "schema_version": "1.0",
        "mode": mode_label,
        "adversarial_mode": ds_prompt is not None,
        "ts_pt": ts_display,
        "ts_iso": now_pt.isoformat(),
        "prompt_chars": len(_effective_prompt),
        "prompt_preview": _effective_prompt[:300] + ("…" if len(_effective_prompt) > 300 else ""),
        "ds_prompt_chars": len(ds_prompt) if ds_prompt is not None else None,
        "gai_prompt_chars": len(gai_prompt) if gai_prompt is not None else None,
        "deepseek": ds_result,
        "gemini": gai_result,
        "summary": {
            "ds_ok": ds_ok,
            "gai_ok": gai_ok,
            "both_ok": ds_ok and gai_ok,
            "partial": ds_ok != gai_ok,
            "both_failed": not ds_ok and not gai_ok,
        },
    }

    _atomic_write_json(out_path, output)
    print(f"[auto_ai_audit] 📄 JSON written: {out_path.name}")

    # In meta-audit mode, also write latest pointer to /var/www/mtf-bot/
    # so the board CCR can fetch it via nginx at /meta_audit_latest.json
    if mode_label == "meta-audit":
        www_path = Path("/var/www/mtf-bot/meta_audit_latest.json")
        try:
            www_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_www = www_path.with_suffix(".tmp")
            tmp_www.write_text(
                json.dumps(output, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp_www.replace(www_path)
            print("[auto_ai_audit] 📄 Board endpoint updated: /meta_audit_latest.json")
        except OSError as exc:
            print(
                f"[auto_ai_audit] ⚠️  Could not write board endpoint: {exc}",
                file=sys.stderr,
            )

        # Push to GitHub Gist so board CCR can fetch without IP allowlist issues
        _push_to_gist(output)

    # ── Print raw responses ───────────────────────────────────────────────
    print()
    print("=" * 72)
    print("DEEPSEEK RESPONSE:")
    print("=" * 72)
    if ds_result["text"]:
        print(ds_result["text"])
    else:
        print(f"[FAILED — {ds_result['error']}]")

    print()
    print("=" * 72)
    print("GEMINI RESPONSE:")
    print("=" * 72)
    if gai_result["text"]:
        print(gai_result["text"])
    else:
        print(f"[FAILED — {gai_result['error']}]")

    print()
    print(f"[auto_ai_audit] Done. Full JSON: {out_path}")

    if post_slack:
        _post_slack_summary(ds_result, gai_result, out_path, mode_label)

    return ds_result, gai_result


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Automated DS/GAI audit gate (patch-gate + meta-audit modes). "
            "Submits identical prompts to DeepSeek + Gemini."
        )
    )
    # ── Patch-gate args ───────────────────────────────────────────────────
    parser.add_argument("--prompt", help="Audit prompt as a string")
    parser.add_argument(
        "--prompt-file", help="Path to a file containing the audit prompt"
    )
    # ── Meta-audit args ───────────────────────────────────────────────────
    parser.add_argument(
        "--meta-audit",
        action="store_true",
        help=(
            "Auto-build cross-review prompt from today's Gemini audit "
            "reports + trade events + bot log. Posts Slack summary."
        ),
    )
    parser.add_argument(
        "--no-slack",
        action="store_true",
        help="Suppress Slack post in --meta-audit mode",
    )
    # ── Shared args ───────────────────────────────────────────────────────
    parser.add_argument(
        "--no-rth-block",
        action="store_true",
        help="Bypass RTH block — TESTING ONLY",
    )
    args = parser.parse_args()

    if not args.no_rth_block:
        _check_rth_block()

    now_pt = datetime.now(_PT)
    ts_file = now_pt.strftime("%Y%m%d_%H%M%S")

    # ── Meta-audit mode ───────────────────────────────────────────────────
    if args.meta_audit:
        print("[auto_ai_audit] 📊 Building meta-audit data context (adversarial mode) ...")
        context, sources = _build_meta_audit_data_context()
        print("[auto_ai_audit] Sources loaded:")
        for k, v in sources.items():
            label = "✅" if "⚠️" not in str(v) and "NOT FOUND" not in str(v) else "⚠️ "
            print(f"  {label} {k}: {v}")
        ds_p = _build_ds_prompt(context)
        gai_p = _build_gai_prompt(context)
        if not ds_p.strip() or not gai_p.strip():
            print(
                "ERROR: Meta-audit prompts are empty — no data context available.",
                file=sys.stderr,
            )
            sys.exit(2)
        out_path = _LOGS_DIR / f"ai_audit_meta_{ts_file}_PT.json"
        ds_result, gai_result = _run_audit(
            "",                          # base prompt unused in adversarial mode
            out_path,
            mode_label="meta-audit",
            post_slack=not args.no_slack,
            ds_prompt=ds_p,
            gai_prompt=gai_p,
        )
        # Append this week's directives to the running log for future compliance tracking
        _week_label = datetime.now(_PT).strftime("%Y-W%W")
        _append_directives_log(
            _week_label,
            ds_result.get("text") if ds_result else None,
            gai_result.get("text") if gai_result else None,
        )
    else:
        # ── Patch-gate mode ───────────────────────────────────────────────
        prompt = _resolve_prompt(args)
        if not prompt:
            print("ERROR: Empty prompt.", file=sys.stderr)
            sys.exit(1)
        out_path = _LOGS_DIR / f"ai_audit_{ts_file}_PT.json"
        ds_result, gai_result = _run_audit(
            prompt,
            out_path,
            mode_label="patch-gate",
            post_slack=False,  # patch-gate: Claude reads stdout, no Slack
        )

    # ── Exit code ─────────────────────────────────────────────────────────
    ds_ok = ds_result["error"] is None
    gai_ok = gai_result["error"] is None
    if not ds_ok and not gai_ok:
        sys.exit(2)
    elif not ds_ok or not gai_ok:
        sys.exit(1)
    # exit 0 implied


if __name__ == "__main__":
    main()
