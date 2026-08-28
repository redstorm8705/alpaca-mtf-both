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
    GROQ_API_KEY        — Groq API key (gsk_...)
    GEMINI_API_KEY      — Google Gemini API key
    SLACK_WEBHOOK_URL   — Slack incoming webhook (meta-audit Slack post)
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

# ── Load .env (required for cron — systemd/cron does not pre-load .env) ──────
from dotenv import load_dotenv
if not load_dotenv():
    print("[auto_ai_audit] WARNING: .env not found — relying on pre-set environment variables", file=sys.stderr)

# ── Absolute path anchors (RC-2 prevention) ───────────────────────────────────
_HERE = Path(__file__).resolve().parent
_LOGS_DIR = _HERE / "logs"

# ── Timezone constants (RC-1 prevention) ─────────────────────────────────────
_ET = ZoneInfo("America/New_York")
_PT = ZoneInfo("America/Los_Angeles")

# ── API constants ─────────────────────────────────────────────────────────────
_GRO_BASE_URL = "https://api.groq.com/openai/v1"
_GRO_MODEL = "openai/gpt-oss-120b"  # was llama-3.3-70b-versatile (DEAD — Groq 404, 2026-08).
# gpt-oss-120b is a REASONING model → the call MUST pass reasoning_effort:"low" and use
# max_completion_tokens (not max_tokens), else hidden reasoning eats the budget → EMPTY content.
_GEMINI_MODEL = "gemini-3.1-flash-lite"  # cost fix 2026-07-11 (Rafael): pro-preview was the
# single premium caller in the whole pipeline and depleted credits in a week — flash is
# what nightly/midday audits already use and is adequate for the meta-audit cross-review.
_API_TIMEOUT_S = 180  # 3-minute wall-clock limit per API call

# ── Meta-audit constants ──────────────────────────────────────────────────────
_BOT_LOG_TAIL_LINES = 100          # bot log lines for system health context
_TRADE_EVENTS_DAYS_BACK = 7        # load ALL events from past 7 days (not just tail)
_FILLS_DAYS_BACK = 7               # Alpaca fills window
_CHART_PROXY_BARS = 30             # daily bars for chart proxy calc per symbol
_DIRECTIVES_HISTORY_WEEKS = 4      # prior audit entries to include for compliance check
_MIN_FILLS_FOR_DIRECTIVES = 20     # N < this → observe only, NO parameter directives

# ── Meta-audit event filtering (2026-07-28) ──────────────────────────────────
# trade_events.jsonl is ~94% high-volume telemetry (delta_shadow/mri_refresh/
# halt_eval/breadth_refresh) — noise for a trade-by-trade audit, and the sole
# unbounded contributor that grew the meta-audit body to ~1M chars (~254k tokens).
# Gemini's 1M-token window swallowed it; Groq's llama-3.3-70b 131k-token window
# hard-rejected it (400) — the Groq cross-review leg was dead 6 days. Excluding
# telemetry keeps only trade-lifecycle events (~38/wk); the cap + Groq-only char
# clamp are backstops so a future volume spike can never re-break the Groq leg.
_META_TELEMETRY_EVENT_TYPES = frozenset({
    "delta_shadow", "mri_refresh", "halt_eval", "breadth_refresh",
})
_META_MAX_EVENTS = 500             # backstop cap (most-recent) after telemetry filter
# Groq free tier ("on_demand") caps gpt-oss-120b at 8,000 tokens/MINUTE (TPM), counting
# input + completion together (verified 2026-08-20 via x-ratelimit-limit-tokens: 8000) — this
# is LOWER than llama-3.3-70b's old 12k TPM, so the input budget + completion cap must together
# clear 8k. ~9k-char prompt (~4.3k tok at ~2.1 ch/tok) + 2500 completion ≈ 6.8k, safely under.
# Gemini (1M window, no TPM issue) keeps the full tail.
_GRO_BOT_LOG_LINES = 15            # Groq-only bot-log tail (vs 100 for Gemini)
_GRO_MAX_COMPLETION_TOKENS = 2500  # bound Groq completion so input+completion ≤ 8k TPM (gpt-oss)
_GRO_PROMPT_CHAR_BUDGET = 9_000    # Groq-only clamp (~4.3k tok at ~2.1 ch/tok); keeps total < 8k TPM

# ── Adversarial role preambles (Round 2 DS/GAI finding — prevent groupthink) ─
_GRO_ROLE_PREAMBLE = (
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
            _bars_params: dict[str, str | int] = {
                "timeframe": "1Day",
                "limit": _CHART_PROXY_BARS,
                "adjustment": "raw",
            }
            resp = requests.get(
                f"https://data.alpaca.markets/v2/stocks/{sym}/bars",
                headers=headers,
                params=_bars_params,
                timeout=15,
            )
            if resp.status_code == 200:
                # RC-6: Alpaca returns {"bars": null} (not []) for a symbol with no
                # data in the window, so `.get("bars", [])` yields None — and a later
                # len(None) crashed the ENTIRE meta-audit (incl. SPY, the benchmark).
                # `or []` coerces null/missing/empty → [] so the symbol degrades to
                # NO_DATA instead of aborting the run.
                bars_data[sym] = resp.json().get("bars") or []
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
    week: str, gro_text: str | None, gai_text: str | None
) -> None:
    """Append this week's audit output to audit_directives.jsonl (atomic write, RC-5 compliant)."""
    path = _LOGS_DIR / "audit_directives.jsonl"
    entry = {
        "week": week,
        "ts_pt": datetime.now(_PT).strftime("%Y-%m-%d %I:%M %p PT"),
        "gro_directives_preview": (gro_text or "")[:2000],
        "gai_directives_preview": (gai_text or "")[:2000],
        # context_only: compliance-tracking record — NOT processable by the patch
        # generator (no file/finding keys). Structured findings are written
        # separately by _append_structured_directives() with status pending_review.
        "status": "context_only",
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


# ── Structured directive extraction (S58 — Stage 1 → Stage 1.5 contract fix) ─
# Board design (Kim/Beck): producer satisfies the consumer's contract natively.
# DS/GAI return a fenced JSON findings array (section 6 of the prompt) parsed
# here; midday/nightly Gemini reports' pipe-delimited NEW BUGS rows are parsed
# deterministically. No extra LLM hop in the critical path.

def _parse_json_findings(text: str | None) -> list[dict]:
    """Extract the LAST fenced ```json array from an LLM response. [] on any failure."""
    if not text:
        return []
    import re
    blocks = re.findall(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if not blocks:
        # fall back: bare JSON array at end of response
        m = re.search(r"(\[\s*\{.*\}\s*\])\s*$", text, re.DOTALL)
        blocks = [m.group(1)] if m else []
    for raw in reversed(blocks):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except json.JSONDecodeError:
            continue
    return []


def _parse_pipe_findings(report_path: Path | None) -> list[dict]:
    """Parse 'NEW BUGS FOUND' pipe-delimited rows from a Gemini report file.
    Row format: '*   CATEGORY | SEVERITY | file.py | title | description'
    Deterministic — no LLM call. [] if file missing or section absent."""
    if report_path is None or not report_path.exists():
        return []
    import re
    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    m = re.search(r"NEW BUGS[^\n]*\n(.*?)(?:\n#{2,}|\n[A-Z ]{8,}\n|\Z)", text, re.DOTALL)
    if not m:
        return []
    findings: list[dict] = []
    for line in m.group(1).splitlines():
        line = line.strip().lstrip("*").strip()
        parts = [p.strip().strip("`") for p in line.split("|")]
        if len(parts) < 4:
            continue
        category, severity, file_field = parts[0], parts[1], parts[2]
        rest = " — ".join(parts[3:])
        if not file_field or "." not in file_field:
            continue  # not a real file reference
        findings.append({
            "file": file_field,
            "finding": rest[:500],
            "recommended_fix": "",
            "rc_class": f"{category}/{severity}"[:40],
        })
    return findings


def _append_structured_directives(
    week: str, gro_text: str | None, gai_text: str | None
) -> dict:
    """Validate + dedup structured findings from all 4 sources and append each as
    its own pending_review line in audit_directives.jsonl. Returns per-source counts."""
    path = _LOGS_DIR / "audit_directives.jsonl"
    import hashlib
    sources = {
        "gro_meta": _parse_json_findings(gro_text),
        "gai_meta": _parse_json_findings(gai_text),
        "nightly_report": _parse_pipe_findings(
            _find_latest_audit_file("gemini_audit_{date}.txt")),
        "midday_report": _parse_pipe_findings(
            _find_latest_audit_file("midday_gemini_{date}.txt")),
    }
    # existing dedup keys (any status — never re-add a finding once seen)
    seen: set = set()
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if d.get("file") and d.get("finding"):
                seen.add(hashlib.sha256(
                    (d["file"] + d["finding"][:120]).encode()).hexdigest())
    counts: dict = {}
    new_entries: list[dict] = []
    rejects = 0
    for src, items in sources.items():
        kept = 0
        for it in items:
            f, finding = str(it.get("file", "")).strip(), str(it.get("finding", "")).strip()
            if not f or not finding:
                rejects += 1
                continue
            if not (_HERE / f).exists():
                rejects += 1
                print(f"[auto_ai_audit] ⚠️  Directive rejected — file not in repo: {f}",
                      file=sys.stderr)
                continue
            key = hashlib.sha256((f + finding[:120]).encode()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            new_entries.append({
                "file": f,
                "finding": finding[:500],
                "recommended_fix": str(it.get("recommended_fix", ""))[:500],
                "rc_class": str(it.get("rc_class", "uncategorized"))[:40],
                "source": src,
                "week": week,
                "ts_pt": datetime.now(_PT).strftime("%Y-%m-%d %I:%M %p PT"),
                "status": "pending_review",
            })
            kept += 1
        counts[src] = kept
    if new_entries:
        try:
            existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                existing + "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in new_entries),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            print(f"[auto_ai_audit] ⚠️  Structured directives write failed: {exc}",
                  file=sys.stderr)
            return {**counts, "write_error": 1}
    counts["validation_rejects"] = rejects
    counts["total_new"] = len(new_entries)
    print(f"[auto_ai_audit] 📋 Structured directives: {counts}")
    return counts


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


def _format_meta_audit_body(
    ctx: dict,
    exclude_telemetry: bool = True,
    max_events: int | None = _META_MAX_EVENTS,
    bot_log_lines: int | None = None,
) -> str:
    """Format shared data sections used by both DS and Gemini prompts.

    exclude_telemetry: drop high-volume telemetry event types (delta_shadow/
        mri_refresh/halt_eval/breadth_refresh) from the TRADE EVENTS render — they
        are ~94% of trade_events.jsonl volume and carry no trade-by-trade signal.
    max_events: after the telemetry filter, render only the most-recent N
        trade-lifecycle events (None = no cap). Stats/score/MRI distributions are
        computed upstream in _compute_trade_stats by event TYPE, so they are already
        telemetry-free and are NOT affected by this render-only filter.
    bot_log_lines: cap the BOT LOG TAIL to the most-recent N lines (None = full).
        The 100-line tail is ~73% of the body; Groq passes a small N to clear its
        12k-TPM free-tier cap, Gemini passes None (its 1M window has no TPM issue).
    """
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
                f"  Gro directives: {d.get('gro_directives_preview', 'N/A')[:600]}",
                f"  GAI directives: {d.get('gai_directives_preview', 'N/A')[:600]}",
                "",
            ]
    else:
        parts += ["=== PRIOR DIRECTIVES: None (first audit run — skip compliance section) ===", ""]

    # ── Trade events with inline chart proxies ────────────────────────────
    events = ctx["events"]
    chart_proxies = ctx["chart_proxies"]
    # Filter high-volume telemetry (delta_shadow/mri_refresh/halt_eval/breadth_refresh)
    # so the body stays inside Groq's 131k-token window; keep trade-lifecycle events.
    if exclude_telemetry:
        _life_events = [e for e in events if e.get("event") not in _META_TELEMETRY_EVENT_TYPES]
    else:
        _life_events = list(events)
    _n_telemetry = len(events) - len(_life_events)
    _n_life_total = len(_life_events)
    # Backstop cap: keep the most-recent max_events (file/append order is chronological).
    if max_events is not None and len(_life_events) > max_events:
        _life_events = _life_events[-max_events:]
    if _life_events:
        parts += [
            f"=== TRADE EVENTS — PAST {_TRADE_EVENTS_DAYS_BACK} DAYS "
            f"(showing {len(_life_events)} of {_n_life_total} trade-lifecycle events; "
            f"{_n_telemetry} telemetry rows excluded: "
            f"delta_shadow/mri_refresh/halt_eval/breadth_refresh) ==="
        ]
        for ev in _life_events:
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
        parts += [
            f"=== TRADE EVENTS: 0 trade-lifecycle events in past {_TRADE_EVENTS_DAYS_BACK} days "
            f"({_n_telemetry} telemetry rows excluded of {len(events)} total) — telemetry-active "
            f"but NO trades this window (distinct from a dead/no-data bot) ===",
            "",
        ]

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
        _tail_lines = bot_tail.splitlines()
        # Cap to most-recent N lines when requested (Groq TPM budget); None = full.
        if bot_log_lines is not None and len(_tail_lines) > bot_log_lines:
            _tail_lines = _tail_lines[-bot_log_lines:]
        bot_tail = "\n".join(_tail_lines)
        parts += [
            f"=== BOT LOG TAIL (last {len(_tail_lines)} lines — system health context) ===",
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
        "6. STRUCTURED FINDINGS (MANDATORY — machine-parsed by the patch pipeline)",
        "   End your response with a fenced ```json code block containing a JSON array.",
        '   Each element: {"file": "<repo-relative path>", "finding": "<specific code-level issue>",',
        '   "recommended_fix": "<concrete fix>", "rc_class": "<RC-1..RC-8 or category>"}.',
        "   ONLY include findings that name a specific repository file with a code-level bug.",
        "   Parameter-tuning ideas, regime observations, and strategy commentary do NOT belong",
        "   here — those go in section 4. If no code-level findings, output [].",
        "",
    ]
    return "\n".join(parts)


def _clamp_prompt_chars(text: str, budget: int) -> str:
    """Last-resort char clamp for the Groq prompt: keep HEAD (70%) + TAIL (30%) so
    the REQUESTED-OUTPUT-FORMAT footer at the END of the body always survives (a
    plain tail-cut would decapitate the instructions and the required ```json block).
    Should be unreachable once telemetry is filtered (post-filter body is <50k chars);
    this only guards against a future firehose event type not yet in the denylist.
    Mutates the INPUT prompt only — never the model response — so it cannot corrupt
    the downstream JSON-findings parser.
    """
    if len(text) <= budget:
        return text
    head_n = int(budget * 0.7)
    tail_n = budget - head_n
    cut = len(text) - head_n - tail_n
    return (
        text[:head_n]
        + f"\n\n[... {cut:,} chars truncated to fit Groq's 131k-token context window ...]\n\n"
        + text[-tail_n:]
    )


def _build_gro_prompt(ctx: dict) -> str:
    """Build Groq prompt: skeptical risk auditor role + shared data context.

    Groq's llama-3.3-70b context is 131k tokens (vs Gemini's 1M), so a hard char
    clamp is applied AFTER telemetry filtering as a defense-in-depth backstop.
    """
    prompt = _GRO_ROLE_PREAMBLE + _format_meta_audit_body(
        ctx, bot_log_lines=_GRO_BOT_LOG_LINES
    )
    if len(prompt) > _GRO_PROMPT_CHAR_BUDGET:
        print(
            f"[auto_ai_audit] ⚠️  Groq prompt {len(prompt):,} chars > budget "
            f"{_GRO_PROMPT_CHAR_BUDGET:,} — clamping (telemetry filter should have "
            f"prevented this; check for a new high-volume event type).",
            file=sys.stderr,
        )
        prompt = _clamp_prompt_chars(prompt, _GRO_PROMPT_CHAR_BUDGET)
    return prompt


def _build_gai_prompt(ctx: dict) -> str:
    """Build Gemini prompt: alpha optimizer role + shared data context.

    No char clamp — Gemini's 1M-token window comfortably fits the (now telemetry-
    filtered) body.
    """
    return _GAI_ROLE_PREAMBLE + _format_meta_audit_body(ctx)


# ── Slack post (meta-audit results) ──────────────────────────────────────────
def _post_slack_summary(
    gro_result: dict,
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

    gro_ok = gro_result["error"] is None
    gai_ok = gai_result["error"] is None
    now_pt = datetime.now(_PT)
    ts = now_pt.strftime("%Y-%m-%d %I:%M %p PT")

    # Terse-classify an API error so a raw provider error blob (e.g. Gemini's full
    # RESOURCE_EXHAUSTED / "prepayment credits depleted" JSON with billing URLs) never
    # gets dumped into Slack — the #1 noise Rafael flagged 2026-08-26. A quota/credit
    # exhaustion (the expected free-tier state) collapses to one plain line; any other
    # error is single-lined and truncated. No URL survives → nothing to unfurl.
    def _terse_error(err: object) -> str:
        e = " ".join(str(err or "unknown error").split())
        low = e.lower()
        if any(k in low for k in (
            "resource_exhausted", "quota", "prepayment", "credits are depleted",
            "429", "rate limit", "rate_limit", "exceeded your current",
        )):
            return "unavailable (free-tier quota/credits exhausted)"
        return e[:120] + ("…" if len(e) > 120 else "")

    # Build short excerpts (first 350 chars of each response)
    def _excerpt(result: dict, label: str) -> str:
        if not result["text"]:
            return f"*{label}:* ❌ {_terse_error(result['error'])}"
        preview = result["text"][:350].replace("\n", " ").strip()
        return f"*{label} (preview):* {preview}…"

    text = (
        f":robot_face: *Auto AI {mode_label.title()} — {ts}*\n"
        f"Gro: {'✅' if gro_ok else '❌'}  |  "
        f"GAI: {'✅' if gai_ok else '❌'}\n\n"
        f"{_excerpt(gro_result, 'Groq')}\n\n"
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


# ── Groq call (OpenAI-compatible REST, no SDK required) ──────────────────────
def _call_groq(prompt: str) -> dict:
    """Submit prompt to Groq API.

    Returns {text, model, tokens, elapsed_s, error}.
    """
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        return {
            "text": None,
            "model": _GRO_MODEL,
            "tokens": None,
            "elapsed_s": 0,
            "error": "GROQ_API_KEY not set in environment",
        }

    import requests  # type: ignore[import-untyped]  # always available

    t0 = time.monotonic()
    try:
        resp = requests.post(
            f"{_GRO_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": _GRO_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                # gpt-oss-120b is a REASONING model. reasoning_effort:"low" keeps the
                # hidden reasoning tokens small (verified ~440 completion tok) so they
                # don't consume the completion budget — a low cap WITHOUT this returns
                # EMPTY content. max_completion_tokens (not the legacy max_tokens) is
                # the reasoning-model param; input+completion stays under the 8k TPM cap.
                "reasoning_effort": "low",
                "max_completion_tokens": _GRO_MAX_COMPLETION_TOKENS,
            },
            timeout=_API_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return {
            "text": text,
            "model": _GRO_MODEL,
            "tokens": usage.get("total_tokens"),
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "text": None,
            "model": _GRO_MODEL,
            "tokens": None,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": str(exc),
        }


# ── Gemini call (google.genai SDK — replaces deprecated google.generativeai) ──
def _call_gemini(prompt: str) -> dict:
    """Submit prompt to Google Gemini API via google.genai SDK.

    Uses _GEMINI_MODEL (gemini-3.1-flash-lite as of 2026-07-11 — cost fix; see the
    constant). google.generativeai is deprecated; google.genai is the current SDK.
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
    gro_prompt: str | None = None,
    gai_prompt: str | None = None,
) -> tuple[dict, dict]:
    """Submit prompt to Groq + Gemini, write JSON, print responses.

    gro_prompt / gai_prompt: adversarial-mode overrides (meta-audit).
    When provided, Groq gets gro_prompt and Gemini gets gai_prompt (different roles).
    When None, both get the shared `prompt` (patch-gate mode).

    Returns (gro_result, gai_result).
    """
    now_pt = datetime.now(_PT)
    ts_display = now_pt.strftime("%Y-%m-%d %I:%M %p PT")

    print(f"[auto_ai_audit] [{mode_label}] — {ts_display}")
    if gro_prompt is not None or gai_prompt is not None:
        print(
            f"[auto_ai_audit] Adversarial mode: "
            f"Gro={len(gro_prompt or ''):,} chars (skeptic) | "
            f"GAI={len(gai_prompt or ''):,} chars (optimizer)"
        )
    else:
        print(f"[auto_ai_audit] Prompt: {len(prompt):,} chars")

    # ── Groq (uses gro_prompt override in adversarial mode) ──────────────
    _gro_call_prompt = gro_prompt if gro_prompt is not None else prompt
    print("[auto_ai_audit] ⏳ Calling Groq ...")
    gro_result = _call_groq(_gro_call_prompt)
    if gro_result["error"]:
        print(
            f"[auto_ai_audit] ⚠️  Groq FAILED "
            f"({gro_result['elapsed_s']}s): {gro_result['error']}",
            file=sys.stderr,
        )
    else:
        print(
            f"[auto_ai_audit] ✅ Groq OK "
            f"({gro_result['elapsed_s']}s, {gro_result['tokens']} tokens)"
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
    gro_ok = gro_result["error"] is None
    gai_ok = gai_result["error"] is None

    _effective_prompt = gro_prompt or prompt
    output = {
        "schema_version": "1.0",
        "mode": mode_label,
        "adversarial_mode": gro_prompt is not None,
        "ts_pt": ts_display,
        "ts_iso": now_pt.isoformat(),
        "prompt_chars": len(_effective_prompt),
        "prompt_preview": _effective_prompt[:300] + ("…" if len(_effective_prompt) > 300 else ""),
        "gro_prompt_chars": len(gro_prompt) if gro_prompt is not None else None,
        "gai_prompt_chars": len(gai_prompt) if gai_prompt is not None else None,
        "gro": gro_result,
        "gemini": gai_result,
        "summary": {
            "gro_ok": gro_ok,
            "gai_ok": gai_ok,
            "both_ok": gro_ok and gai_ok,
            "partial": gro_ok != gai_ok,
            "both_failed": not gro_ok and not gai_ok,
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

        # S47e: Write local meta_audit_latest.json as guaranteed fallback.
        # /var/www/mtf-bot/ may not exist; Gist requires GITHUB_GIST_TOKEN.
        # This ensures board CCR always reads current data via logs/ path.
        _local_latest = _LOGS_DIR / "meta_audit_latest.json"
        try:
            _atomic_write_json(_local_latest, output)
            print(f"[auto_ai_audit] 📄 Local latest pointer written: {_local_latest.name}")
        except Exception as _mle:  # noqa: BLE001
            print(
                f"[auto_ai_audit] ⚠️  Local latest pointer write failed: {_mle}",
                file=sys.stderr,
            )

    # ── Print raw responses ───────────────────────────────────────────────
    print()
    print("=" * 72)
    print("GROQ RESPONSE:")
    print("=" * 72)
    if gro_result["text"]:
        print(gro_result["text"])
    else:
        print(f"[FAILED — {gro_result['error']}]")

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
        _post_slack_summary(gro_result, gai_result, out_path, mode_label)

    return gro_result, gai_result


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
        gro_p = _build_gro_prompt(context)
        gai_p = _build_gai_prompt(context)
        if not gro_p.strip() or not gai_p.strip():
            print(
                "ERROR: Meta-audit prompts are empty — no data context available.",
                file=sys.stderr,
            )
            sys.exit(2)
        out_path = _LOGS_DIR / f"ai_audit_meta_{ts_file}_PT.json"
        gro_result, gai_result = _run_audit(
            "",                          # base prompt unused in adversarial mode
            out_path,
            mode_label="meta-audit",
            post_slack=not args.no_slack,
            gro_prompt=gro_p,
            gai_prompt=gai_p,
        )
        # Append this week's directives to the running log for future compliance tracking
        _week_label = datetime.now(_PT).strftime("%Y-W%W")
        _append_directives_log(
            _week_label,
            gro_result.get("text") if gro_result else None,
            gai_result.get("text") if gai_result else None,
        )
        # S58: extract structured findings → pending_review directives for Stage 1.5
        _counts = _append_structured_directives(
            _week_label,
            gro_result.get("text") if gro_result else None,
            gai_result.get("text") if gai_result else None,
        )
        # Majors instrumentation: zero extracted findings across ALL sources while
        # DS/GAI both succeeded = likely format drift, not a clean week. Alert.
        _src_total = sum(
            v for k, v in _counts.items()
            if k not in ("validation_rejects", "total_new", "write_error")
        )
        if _src_total == 0 and gro_result.get("text") and gai_result.get("text"):
            _wh = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
            if _wh:
                import requests  # type: ignore[import-untyped]
                try:
                    requests.post(_wh, json={"text": (
                        "⚠️ *Directive extraction yielded 0 findings from all 4 sources* "
                        "while DS+GAI both responded. Possible format drift — check "
                        "section-6 JSON blocks in ai_audit_meta output and NEW BUGS "
                        "sections in gemini_audit/midday_gemini reports."
                    )}, timeout=10)
                except Exception as _se:  # noqa: BLE001
                    print(f"[auto_ai_audit] ⚠️  Zero-findings alert failed: {_se}",
                          file=sys.stderr)
    else:
        # ── Patch-gate mode ───────────────────────────────────────────────
        prompt = _resolve_prompt(args)
        if not prompt:
            print("ERROR: Empty prompt.", file=sys.stderr)
            sys.exit(1)
        out_path = _LOGS_DIR / f"ai_audit_{ts_file}_PT.json"
        gro_result, gai_result = _run_audit(
            prompt,
            out_path,
            mode_label="patch-gate",
            post_slack=False,  # patch-gate: Claude reads stdout, no Slack
        )

    # ── Exit code ─────────────────────────────────────────────────────────
    gro_ok = gro_result["error"] is None
    gai_ok = gai_result["error"] is None
    if not gro_ok and not gai_ok:
        sys.exit(2)
    elif not gro_ok or not gai_ok:
        sys.exit(1)
    # exit 0 implied


if __name__ == "__main__":
    main()
