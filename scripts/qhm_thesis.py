#!/usr/bin/env python3
# ruff: noqa: E501
"""scripts/qhm_thesis.py — QHM WEEKLY FULL-THESIS routine (buy-the-dip-into-earnings research).

Reconstitution of the old Claude Code Remote (CCR) quarterly-holds research routine, which ran on
Claude usage + 4 Claude subagents and failed 25/30 on usage limits. This version runs as an IN-REPO
OCI weekly cron on the box's own compute + the Gro (Groq) + GAI (Gemini) keys in .env — ZERO Claude
usage, so no usage-limit failure. Design record: logs/design_records/qhm_thesis_routine_2026-09-05.md.

Produces (per rules/slack_format.md SLK01-SLK15 for the Slack half):
  - logs/quarterly_holds_research_<YYYY-MM-DD>.md   full rich memo (atomic write)
  - a Block-Kit Slack post (headline + per-candidate + current-holds refresh + cap check)
It is a DRAFT for Rafael + Claude to finalize in-session (the high-conviction call stays human+Claude).

Comprehensive per-candidate research — CCR-parity rebuild 2026-09-06 (100% of the old CCR routine's
elements; OCI has no WebSearch/Claude-subagents, so those become HTTP fetches + Gro/GAI 4-lens):
  - earnings date + holding period    FMP /stable/earnings-calendar (enter post-earnings → day before next)
  - analyst guidance signal           FMP /stable/grades-consensus
  - dip-into-earnings screen          Alpaca daily bars -> %vs50dMA / %off-20d-high / RSI14
  - REAL named-superinvestor 13F      Dataroma stock page (fetched holders, e.g. Buffett/Tepper/Coleman;
                                      an uncovered name shows 'none tracked' — NEVER fabricated, Rafael 2026-09-06)
  - 4-lens board thesis               Gro + GAI (AB/BoD/TB/Execution) → EXEC SUMMARY (2-3 picks) + PER-CANDIDATE
                                      (LLM-SYNTHESIZED earnings read, tailwind, thesis, risk) + BOARD VOTE
                                      (stops / intraday coexistence / entry-timing) + CURRENT-HOLDS refresh
  - QHM cap check (aggregate 40% / per-name 20% — aligned to the ENFORCED code cap #273; WARNING/sizing only,
    never an auto-trim; grandfathered over-cap holds are flagged, not sold — Rafael 2026-09-05)
  - pending-approvals scan (CCR secondary task; read-only, never applied)

Data tiers: T1 Alpaca (read-only) · T2 FMP · Dataroma (free 13F) · Gro/GAI (reasoning). NO order execution,
NO broker calls, NO data/state writes. Read-only research -> logs/ + Slack. paper=True untouched.
Usage: python3 scripts/qhm_thesis.py [--dry-run]   (--dry-run: build memo, skip Slack send)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Populate ALPACA_*/FMP_API_KEY/GROQ_API_KEY/GEMINI_API_KEY from the repo .env, as every cron does.
_ROOT = Path(__file__).resolve().parent.parent
# Run as `python3 scripts/qhm_thesis.py` puts scripts/ (not the repo root) on sys.path[0], so root
# modules (config, alerts, gai_client) fail to import. Put the repo root first so they resolve.
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass
except Exception as _e:  # a present-but-unreadable .env must not crash at import (RC-3)
    logging.getLogger("qhm_thesis").warning("load_dotenv skipped (%s) — ambient env", _e)

logger = logging.getLogger("qhm_thesis")

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")

_FMP = "https://financialmodelingprep.com/stable"
_ALPACA_DATA = "https://data.alpaca.markets/v2/stocks"
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_MODEL = "openai/gpt-oss-120b"          # llama-3.3-70b was retired by Groq (2026); gpt-oss-120b is the live large chat model
_TIMEOUT = 20.0

# Candidate universe beyond the live scan list (config.WATCHLIST is added at runtime).
_BEYOND_SCAN = {
    "Memory/Semis":      ["MU", "AMAT", "LRCX", "KLAC", "ON", "MRVL", "AVGO", "SNDK"],
    "AI infrastructure": ["DELL", "HPE", "VRT", "ETN", "AXON", "ANET"],
    "Defense":           ["LMT", "RTX", "NOC", "GD"],
    "Energy transition": ["CEG", "VST", "NRG", "GEV"],
    "Healthcare":        ["LLY", "UNH", "ABBV", "ISRG"],
}
_TOP_INVESTORS = ["Druckenmiller", "Ackman", "Cohen", "Tepper", "Buffett", "Einhorn"]

# QHM sizing caps — WARNING signal for NEW buys, never an auto-trim. Aligned 2026-09-06 to the
# ENFORCED code cap in execution/quarterly_hold_manager.py (#273: 40% aggregate / 20% per-name);
# the warning must match what the live entry path actually enforces.
_QHM_AGG_CAP_PCT = 0.40
_QHM_NAME_CAP_PCT = 0.20
# Dip screen: a buyable pullback into a catalyst.
_DIP_VS50_MAX = 3.0      # within +3% of the 50d MA (not extended)
_DIP_OFFHIGH_MAX = -4.0  # >=4% off the 20d high
_EARN_LOOKAHEAD_DAYS = 55  # "next month" + runway
_SHORTLIST_N = 10          # enrich + board this many top dips


def _http_json(url: str, headers: dict | None = None, timeout: float = _TIMEOUT):
    """GET url -> parsed JSON, or None on ANY failure (never raises — every source is best-effort)."""
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        logger.debug("http_json failed (%s): %s", url.split("?")[0], e)
        return None


# ── Data helpers (each fail-safe -> None/{} so a dead source only drops its enrichment) ──────────
def _fmp_key() -> str:
    return os.getenv("FMP_API_KEY", "").strip()


def _earnings_calendar(date_from: str, date_to: str) -> dict[str, str]:
    """{SYMBOL: 'YYYY-MM-DD'} nearest earnings in the window (FMP /stable/earnings-calendar)."""
    key = _fmp_key()
    if not key:
        return {}
    url = f"{_FMP}/earnings-calendar?{urllib.parse.urlencode({'from': date_from, 'to': date_to, 'apikey': key})}"
    data = _http_json(url)
    out: dict[str, str] = {}
    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):   # skip a malformed non-dict row (never-crash, same guard as _positions)
                continue
            s = (row.get("symbol") or "").upper()
            d = row.get("date")
            if not (s and d):
                continue
            d_str = str(d)[:10]   # coerce BEFORE compare so a stray int date can't TypeError vs a str
            if s not in out or d_str < out[s]:  # keep the NEAREST future date
                out[s] = d_str
    return out


def _grades(symbol: str) -> dict:
    """Analyst consensus (FMP /stable/grades-consensus) — a guidance/sentiment proxy (transcripts premium)."""
    key = _fmp_key()
    if not key:
        return {}
    data = _http_json(f"{_FMP}/grades-consensus?{urllib.parse.urlencode({'symbol': symbol, 'apikey': key})}")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + ag / al), 0)


def _dip_metrics(symbol: str) -> dict:
    """Alpaca daily bars -> {last, vs50, vs20, off20h, rsi} (%). {} on failure. feed=iex (free tier)."""
    kid, sec = os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_SECRET_KEY", "")
    if not kid or not sec:
        return {}
    start = (datetime.now(timezone.utc) - timedelta(days=95)).strftime("%Y-%m-%d")
    q = urllib.parse.urlencode({"timeframe": "1Day", "start": start, "limit": 70, "adjustment": "split", "feed": "iex"})
    data = _http_json(f"{_ALPACA_DATA}/{symbol}/bars?{q}",
                      headers={"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec})
    bars = (data.get("bars") if isinstance(data, dict) else None) or []   # non-dict (e.g. a list) -> []
    # Filter to NUMERIC values (not just key-present): a bar can carry {"c": null} for a halted symbol,
    # which "c" in b would admit and then sum([...None...]) would crash on. isinstance excludes None/str.
    closes = [b["c"] for b in bars if isinstance(b, dict) and isinstance(b.get("c"), (int, float))]
    highs = [b["h"] for b in bars if isinstance(b, dict) and isinstance(b.get("h"), (int, float))]
    if len(closes) < 25 or len(highs) < 20:   # need enough closes (MA/RSI) AND highs (20d high) — a
        return {}                              # partial bars response (c but no h) must NOT reach max([])
    last = closes[-1]
    sma50 = sum(closes[-50:]) / min(50, len(closes))
    sma20 = sum(closes[-20:]) / 20
    high20 = max(highs[-20:])
    return {
        "last": round(last, 2),
        "vs50": round((last / sma50 - 1) * 100, 1),
        "vs20": round((last / sma20 - 1) * 100, 1),
        "off20h": round((last / high20 - 1) * 100, 1),
        "rsi": _rsi(closes),
    }


_DATAROMA_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _dataroma_13f(symbol: str) -> dict:
    """Real named-superinvestor 13F holdings from Dataroma (free, public; ~80 curated funds like
    Buffett/Berkshire, Tepper/Appaloosa, Coleman/Tiger Global). Returns {'count': int|None,
    'holders': [names]} or {} on any failure / uncovered symbol.

    HARD RULE (Rafael 2026-09-06): REAL fetched data ONLY — NEVER fabricated. An uncovered ticker
    (verified: Dataroma returns no holder links for one) yields {} so the memo prints 'not tracked',
    and no LLM is ever asked to invent a 13F holding. Needs a full browser UA (Dataroma blocks a
    bare urllib UA)."""
    try:
        req = urllib.request.Request(
            f"https://www.dataroma.com/m/stock.php?sym={urllib.parse.quote(symbol)}",
            headers={"User-Agent": _DATAROMA_UA})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception as e:
        logger.debug("dataroma fetch failed for %s: %s", symbol, e)
        return {}
    try:
        m = re.search(r"Ownership count:</td><td><b>(\d+)</b>", html)
        count = int(m.group(1)) if m else None
        names = re.findall(r"holdings\.php\?m=[A-Za-z0-9]+[^>]*>([^<]+)</a>", html)
        holders = list(dict.fromkeys(n.strip() for n in names if n.strip()))[:12]
    except Exception as e:   # parse-shape change must not crash the run (never-fabricate = return {})
        logger.debug("dataroma parse failed for %s: %s", symbol, e)
        return {}
    if count is None and not holders:
        return {}   # not covered by the superinvestor set — honest empty, no fabrication
    return {"count": count, "holders": holders}


def _pending_approvals() -> list[str]:
    """CCR-parity secondary task: names of any logs/pending_approvals_*.md awaiting Rafael (read-only
    — never applies them). [] if none / on error."""
    try:
        return sorted(p.name for p in (_ROOT / "logs").glob("pending_approvals_*.md"))
    except Exception as e:
        logger.debug("pending-approvals scan failed: %s", e)
        return []


def _gro(prompt: str, system: str) -> str | None:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return None
    body = json.dumps({"model": _GROQ_MODEL,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                       "max_tokens": 3500, "temperature": 0.2}).encode()
    try:
        req = urllib.request.Request(_GROQ_URL, data=body,
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                              # Cloudflare in front of Groq 403s a UA-less urllib request
                                              # (known QHM/Groq-UA gotcha) — send a browser UA so it passes.
                                              "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read().decode())
        return d["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("Gro call failed: %s", e)
        return None


def _gai(prompt: str) -> str | None:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    try:
        from gai_client import call_gai
        return call_gai(prompt, key, max_output_tokens=4096)
    except Exception as e:
        logger.warning("GAI call failed: %s", e)
        return None


# ── current holds (read-only) ────────────────────────────────────────────────────────────────
def _current_holds() -> list[str]:
    """QHM hold symbols from data/state/quarterly_holds.json (intent). [] on any failure."""
    try:
        d = json.loads((_ROOT / "data" / "state" / "quarterly_holds.json").read_text())
        positions = d.get("positions", d) if isinstance(d, dict) else {}
        return sorted(k for k in positions.keys()) if isinstance(positions, dict) else []
    except Exception as e:
        logger.debug("current holds read failed: %s", e)
        return []


def _positions() -> dict[str, float]:
    """{SYMBOL: market_value} from Alpaca paper positions (read-only GET). {} on any failure —
    used only to compute %-of-equity for the QHM cap WARNING (never trades)."""
    kid, sec = os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_SECRET_KEY", "")
    if not kid or not sec:
        return {}
    data = _http_json("https://paper-api.alpaca.markets/v2/positions",
                      headers={"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec})
    out: dict[str, float] = {}
    if isinstance(data, list):
        for p in data:
            if not isinstance(p, dict):   # a non-dict list element (malformed response) must not
                continue                  # reach p.get() — same never-crash guard as _dip_metrics
            s = (p.get("symbol") or "").upper()
            mv = p.get("market_value")
            if mv is None:
                continue
            try:
                out[s] = float(mv)
            except (TypeError, ValueError):
                continue
    return out


def _universe() -> list[str]:
    syms: list[str] = []
    try:
        import config
        syms += list(getattr(config, "WATCHLIST", []))
    except Exception as e:
        logger.debug("WATCHLIST import failed: %s", e)
    for names in _BEYOND_SCAN.values():
        syms += names
    syms += _current_holds()
    # dedup, preserve order, drop leveraged/inverse ETFs (not quarterly-hold candidates)
    skip = {"TQQQ", "SQQQ", "SOXL", "SOXS", "TSLL", "NVDL", "SPY", "QQQ", "SMH"}
    seen: dict[str, None] = {}
    for s in syms:
        u = str(s).upper()
        if u and u not in skip and u not in seen:
            seen[u] = None
    return list(seen.keys())


# ── Slack Block Kit (rules/slack_format.md SLK01-SLK15) ─────────────────────────────────────────
def _sec(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}


def _ctx(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:2900]}]}


def _hdr(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": str(text)[:150], "emoji": True}}


_DIV = {"type": "divider"}


def _dip_glyph(m: dict) -> str:
    """🟢 buyable dip / 🟡 mild / ⚪ extended — paired with the number (SLK08)."""
    off = m.get("off20h")
    vs50 = m.get("vs50")
    if off is None or vs50 is None:
        return "⚪"
    if vs50 <= _DIP_VS50_MAX and off <= _DIP_OFFHIGH_MAX:
        return "🟢"
    if off <= -3.0:
        return "🟡"
    return "⚪"


def build_slack(today: str, ranked: list[dict], holds: list[dict], equity: float | None,
                board_gro: bool, board_gai: bool) -> tuple[list, str]:
    """Return (blocks, fallback). SLK-compliant: header -> headline -> divider -> candidates ->
    holds refresh -> footer. send_slack_blocks chunks at <=45 blocks."""
    n = len(ranked)
    top = ranked[0]["sym"] if ranked else "—"
    fallback = f"🔬 QHM Thesis {today} · {n} dip candidate(s) · top {top}"
    if not (board_gro or board_gai):
        fallback += " · ⚠️ board degraded"
    blocks: list = [_hdr(f"QHM Weekly Thesis · {today}")]
    eq_str = f"${equity:,.0f}" if equity else "—"
    board_str = "Gro+GAI" if (board_gro and board_gai) else ("Gro" if board_gro else ("GAI" if board_gai else "⚠️ none"))
    blocks.append(_ctx(f"Equity {eq_str} · {n} dip-into-earnings candidate(s) · board: {board_str} · DRAFT for review"))
    blocks.append(_DIV)
    blocks.append(_sec("*Buy-the-dip candidates* (into an earnings catalyst, ranked by dip depth)"))
    for r in ranked[:12]:
        m = r["m"]
        g = _dip_glyph(m)
        # SLK05: one idea per short line (~30-40 chars), glyph-led, bold only the scannable value.
        line1 = f"{g} *{r['sym']}*  *{m.get('off20h','?')}%* off high"
        line2 = f"earn {r.get('earn','?')} · RSI {m.get('rsi','?')}"
        line3 = f"vs50 {m.get('vs50','?')}% · {r.get('grade','?')}"
        blocks.append(_sec(f"{line1}\n{line2}\n{line3}"))
        ctx = f"last ${m.get('last','?')}"
        if r.get("dr_count"):
            _top = ", ".join(r.get("dr_holders", [])[:3])
            ctx += f" · 13F {r['dr_count']} funds" + (f" ({_top})" if _top else "")
        if r.get("sector"):
            ctx += f" · {r['sector']}"
        blocks.append(_ctx(ctx))
    if holds:
        blocks.append(_DIV)
        blocks.append(_sec("*Current holds — thesis + cap check*"))
        for h in holds:
            cap_flag = " · ⚠️ OVER CAP" if h.get("over_cap") else ""
            blocks.append(_sec(f"• *{h['sym']}* {h.get('pct_eq','?')}% of equity{cap_flag}\n{h.get('dip','')}"))
    blocks.append(_DIV)
    blocks.append(_sec("*Board read* (draft — full thesis in the memo)"))
    blocks.append(_sec(top_board_summary(ranked)))
    blocks.append(_DIV)
    blocks.append(_ctx(f"Full memo: logs/quarterly_holds_research_{today}.md · "
                       f"{datetime.now(PT).strftime('%b %d · %I:%M %p PT')} · cap {int(_QHM_AGG_CAP_PCT*100)}% agg / {int(_QHM_NAME_CAP_PCT*100)}% name"))
    return blocks, fallback


def top_board_summary(ranked: list[dict]) -> str:
    """A short scannable board verdict for Slack (the full board text lives in the memo)."""
    for r in ranked:
        v = r.get("board_verdict")
        if v:
            return v[:700]
    return "_Board section pending (LLM voices unavailable this run) — see memo._"


# ── Board (Gro + GAI, 4-lens) ──────────────────────────────────────────────────────────────────
_GRO_SYS = (
    "You are a 4-seat investment board reviewing quarterly-hold (multi-week to multi-month) LONG "
    "candidates for a $2.5k->$25k PAPER account. Seats, each grounded in its members' documented work:\n"
    "  - AB (Analytics): Thorp (Kelly sizing), Dalio (regime/all-weather), Asness (value+momentum), Brandt (technical R:R)\n"
    "  - BoD: Simons (signal), Taleb (tail/convexity), Kyle (microstructure), Shaw (stat-arb)\n"
    "  - TB (Technical): McKinney (data integrity), Beck (integration), Derman (model risk)\n"
    "  - Execution: Harris (entry/exit mechanics), Levitt (compliance)\n"
    "Rules: ground each seat in documented frameworks; no hedging; directional answers; DRAFT for human "
    "(Rafael+Claude) review, not an auto-trade.\n"
    "13F HARD RULE (Rafael): the holder list given for each name is EXHAUSTIVE and really-fetched from Dataroma. "
    "CITE the notable holders BY NAME (Rafael wants to see WHO holds it) — but ONLY names copied VERBATIM from "
    "that name's list. Adding ANY fund not in the list (even a plausible one from your own memory) is a "
    "HALLUCINATION and a hard review failure. If a name lists NONE, write 'none tracked'. Do NOT 'correct' or "
    "supplement the list.\n"
    "Any earnings-call read must be labeled LLM-SYNTHESIZED (from your training knowledge + the analyst grade), "
    "NOT quoted as a live transcript.\n"
    "Return EXACTLY these sections, with these exact headers:\n"
    "## EXECUTIVE SUMMARY\n(the 2-3 STRONGEST buys — 2 or 3, not more — one-line thesis each)\n"
    "## PER-CANDIDATE\n(for EACH pick: Sector | last-earnings read [LLM-SYNTHESIZED: guidance direction / margin / "
    "TAM] | structural tailwind | 13F corroboration [name the notable holders, ONLY verbatim from that name's "
    "EXHAUSTIVE fetched list] | thesis | key risk | holding window [enter post-earnings, hold to the day "
    "before next earnings])\n"
    "## BOARD VOTE\n"
    "AB: R:R + Kelly %-equity per pick. BoD: is the edge alpha or beta; regime/tail risk over the hold. "
    "TB: integration with the intraday bot — should the bot AVOID intraday trades on a hold symbol; stop criteria. "
    "Execution: entry timing (immediately post-earnings vs wait 1-2 confirmation days); stop-loss design (hard -15% "
    "vs true quarterly hold); coexistence with the intraday book.\n"
    "## CURRENT HOLDS\n(ADD/HOLD/TRIM/EXIT per hold — flag-only; NEVER auto-trim; grandfathered over-cap holds are "
    "HELD per Rafael, not sold)."
)


def _board_prompt(ranked: list[dict], holds: list[dict], equity: float | None) -> str:
    eq = f"${equity:,.0f}" if equity else "~$2,500"
    lines = [f"ACCOUNT: PAPER, CURRENT equity {eq} (~4x margin buying power). GOAL grow to $25,000 — $25k is the "
             f"TARGET, NOT the current balance. Size every recommendation against the CURRENT {eq}.",
             "",
             "CANDIDATES (buy-the-dip-into-earnings; dip metrics + analyst grade + earnings date + REAL 13F holders). "
             "The 13F list per name is EXHAUSTIVE and fetched — cite only those exact names, never add a fund:"]
    for r in ranked[:_SHORTLIST_N]:
        m = r["m"]
        drh = "; ".join(r.get("dr_holders", [])) or "NONE tracked"
        lines.append(f"- {r['sym']} ({r.get('sector','')}): earn {r.get('earn','?')}, off-high {m.get('off20h')}%, "
                     f"vs50 {m.get('vs50')}%, RSI {m.get('rsi')}, grade {r.get('grade','?')}; "
                     f"13F superinvestors [EXHAUSTIVE, {r.get('dr_count') or '?'}]: {drh}")
    lines.append("\nCurrent holds (do NOT auto-trim; flag over-cap only): " +
                 (", ".join(f"{h['sym']} {h.get('pct_eq','?')}%eq" for h in holds) or "none"))
    lines.append(f"\nQHM caps: aggregate {int(_QHM_AGG_CAP_PCT*100)}% of equity, per-name {int(_QHM_NAME_CAP_PCT*100)}% "
                 "(WARNING/sizing only; grandfathered over-cap holds are HELD, not trimmed). Produce the sections "
                 "defined in the system prompt. Be concrete; cite the metrics and only the listed 13F holders.")
    return "\n".join(lines)


def _extract_section(text: str | None, header: str) -> str | None:
    """Pull one '## HEADER' section body out of a board LLM response (for the Slack exec-summary). None if absent."""
    if not text:
        return None
    m = re.search(rf"##\s*{re.escape(header)}\s*\n(.+?)(?:\n##\s|\Z)", text, re.S | re.I)
    return m.group(1).strip() if m else None


def run_board(ranked: list[dict], holds: list[dict], equity: float | None) -> tuple[str | None, str | None]:
    prompt = _board_prompt(ranked, holds, equity)
    gro = _gro(prompt, _GRO_SYS)
    gai = _gai(_GRO_SYS + "\n\n" + prompt)
    return gro, gai


# ── Memo ───────────────────────────────────────────────────────────────────────────────────────
def build_memo(today: str, ranked: list[dict], holds: list[dict], equity: float | None,
               gro: str | None, gai: str | None) -> str:
    pend = _pending_approvals()
    agg, name = int(_QHM_AGG_CAP_PCT * 100), int(_QHM_NAME_CAP_PCT * 100)
    lines = [f"# QHM Weekly Thesis — {today} (buy-the-dip into earnings)",
             f"_Generated {datetime.now(PT).strftime('%Y-%m-%d %I:%M %p PT')} · equity "
             f"{('$'+format(equity, ',.0f')) if equity else '—'} · DRAFT for in-session review_",
             ""]
    if pend:
        lines += [f"> ⚠️ Pending approvals awaiting Rafael: {', '.join(pend)} — read-only note, NOT applied here.", ""]
    lines += [
        "## Methodology",
        "- **Universe:** config.WATCHLIST + a curated S&P-500 candidate set (semis / AI-infra / defense / "
        "energy-transition / healthcare) + current QHM holds; leveraged/inverse ETFs excluded.",
        f"- **Screen:** Alpaca IEX daily bars → dip-into-earnings (within +{_DIP_VS50_MAX:.0f}% of the 50d MA AND "
        f"≥{abs(_DIP_OFFHIGH_MAX):.0f}% off the 20d high), gated on an FMP earnings date in the next ~{_EARN_LOOKAHEAD_DAYS}d.",
        "- **Enrichment:** FMP analyst grade; **Dataroma real named-superinvestor 13F holders** (fetched — an "
        "uncovered name shows 'none tracked', NEVER fabricated). "
        "Earnings-call reads are **LLM-SYNTHESIZED** (transcripts are FMP-premium-blocked), never quoted as live.",
        "- **Board:** Gro + GAI, each running the 4 lenses (AB / BoD / TB / Execution) — DRAFT for human review.",
        "- **Holding window:** enter post-earnings, hold to the day before the next earnings print.",
        "",
        "## Candidate screen (dip into an earnings catalyst)",
        "| Sym | Sector | Earnings | last | vs50 | off-20d-high | RSI | analyst | 13F superinvestors (real, Dataroma) |",
        "|---|---|---|---|---|---|---|---|---|"]
    for r in ranked:
        m = r["m"]
        drh = ", ".join(r.get("dr_holders", [])[:4]) or "none tracked"
        drc = r.get("dr_count")
        drcell = f"{drc} funds: {drh}" if drc else drh
        lines.append(f"| {r['sym']} | {r.get('sector','')} | {r.get('earn','?')} | {m.get('last','?')} | "
                     f"{m.get('vs50','?')}% | {m.get('off20h','?')}% | {m.get('rsi','?')} | {r.get('grade','?')} | {drcell} |")
    lines += ["", "## Board thesis — Gro (AB / BoD / TB / Execution)", gro or "_Gro unavailable this run._",
              "", "## Board thesis — GAI (cross-check)", gai or "_GAI unavailable this run._",
              "", "> **13F note:** holder names are really-fetched from Dataroma (the candidate table is the complete "
              "set per name); the board cites from that fetched list. Any fund NOT in the table is unverified — "
              "disregard it.",
              "", "## Current holds — thesis refresh + cap check"]
    for h in holds:
        flag = " **⚠️ OVER PER-NAME CAP**" if h.get("over_cap") else ""
        lines.append(f"- **{h['sym']}** — {h.get('pct_eq','?')}% of equity{flag}. {h.get('dip','')}")
    lines += ["", "## Next steps",
              "- Rafael + Claude finalize the 2-3 picks in-session (this is a draft).",
              f"- New QHM buys are Kelly-right-sized within the caps (agg {agg}% / per-name {name}%).",
              "- Over-cap grandfathered holds are flagged, NOT auto-trimmed (Rafael 2026-09-05).",
              "- 13F holdings are really-fetched from Dataroma; 'none tracked' = uncovered by the superinvestor set, "
              "never fabricated.",
              ""]
    return "\n".join(lines)


# ── main ─────────────────────────────────────────────────────────────────────────────────────
def _equity() -> float | None:
    kid, sec = os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_SECRET_KEY", "")
    if not kid or not sec:
        return None
    data = _http_json("https://paper-api.alpaca.markets/v2/account",
                      headers={"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec})
    val = data.get("equity") if isinstance(data, dict) else None   # non-dict (e.g. a list) -> None
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    dry = "--dry-run" in sys.argv
    today = datetime.now(PT).strftime("%Y-%m-%d")
    logger.info("qhm_thesis start (%s)%s", today, " [dry-run]" if dry else "")

    equity = _equity()
    holds_syms = _current_holds()
    universe = _universe()

    # 1) earnings window (nearest earnings per symbol in the next ~55 days)
    d_from = datetime.now(ET).strftime("%Y-%m-%d")
    d_to = (datetime.now(ET) + timedelta(days=_EARN_LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")
    earn = _earnings_calendar(d_from, d_to)

    # 2) dip screen over the universe (Alpaca bars); keep names with a real dip into a known catalyst
    cands: list[dict] = []
    for sym in universe:
        m = _dip_metrics(sym)
        if not m:
            continue
        e = earn.get(sym)
        # candidate = dipping AND has an earnings catalyst in-window (or is a current hold to refresh)
        is_dip = (m.get("vs50") is not None and m["vs50"] <= _DIP_VS50_MAX
                  and m.get("off20h") is not None and m["off20h"] <= _DIP_OFFHIGH_MAX)
        if e and is_dip:
            cands.append({"sym": sym, "m": m, "earn": e,
                          "sector": _sector_of(sym)})
    # rank by dip depth (most negative off-high first)
    cands.sort(key=lambda c: c["m"].get("off20h", 0.0))
    ranked = cands[:_SHORTLIST_N]

    # 3) enrich the shortlist (grades + real 13F) — bounded to the shortlist to cap API calls
    for r in ranked:
        g = _grades(r["sym"])
        r["grade"] = g.get("consensus") or "—"
        dr = _dataroma_13f(r["sym"])                        # REAL named superinvestor holders (fetched)
        r["dr_count"] = dr.get("count")
        r["dr_holders"] = dr.get("holders") or []

    # 4) current holds refresh rows (dip metrics + REAL per-name cap check).
    # pct_eq = market_value / equity from Alpaca positions; over_cap flags the per-name cap (20%, enforced #273).
    # WARNING-only (never auto-trims — grandfathered over-cap holds like LLY/GEV are flagged, not sold).
    posmap = _positions()
    holds_rows: list[dict] = []
    for sym in holds_syms:
        m = _dip_metrics(sym)
        row: dict = {"sym": sym,
                     "dip": (f"dip {m.get('off20h')}% off high, RSI {m.get('rsi')}" if m else "no price data")}
        mv = posmap.get(sym)
        if mv is not None and equity and equity > 0:
            pe = mv / equity * 100.0
            row["pct_eq"] = f"{pe:.0f}"
            row["over_cap"] = pe > (_QHM_NAME_CAP_PCT * 100)   # per-name cap warning (real value)
        else:
            row["pct_eq"] = "?"
            row["over_cap"] = False   # no market value → cannot assert over-cap (avoid a false alarm)
        holds_rows.append(row)

    # 5) board (Gro + GAI) on the shortlist + holds
    gro, gai = (None, None)
    if ranked:
        gro, gai = run_board(ranked, holds_rows, equity)
    if gro and ranked:
        exec_sum = _extract_section(gro, "EXECUTIVE SUMMARY")
        ranked[0]["board_verdict"] = (exec_sum or gro.strip().split("\n\n")[0])[:700]

    # 6) memo (atomic) + Slack
    memo = build_memo(today, ranked, holds_rows, equity, gro, gai)
    out_path = _ROOT / "logs" / f"quarterly_holds_research_{today}.md"
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(memo)
        tmp.replace(out_path)
        logger.info("qhm_thesis: wrote %s", out_path)
    except Exception as e:
        logger.error("qhm_thesis: memo write failed: %s", e)

    blocks, fallback = build_slack(today, ranked, holds_rows, equity, bool(gro), bool(gai))
    if dry:
        logger.info("qhm_thesis: dry-run — Slack NOT sent. fallback=%s | %d blocks", fallback, len(blocks))
        return 0
    try:
        from alerts import send_slack_blocks
        send_slack_blocks(blocks, fallback)   # honest logging: send_slack_blocks logs its own failures per part
        logger.info("qhm_thesis: Slack dispatched (%d candidates, %d blocks)", len(ranked), len(blocks))
    except Exception as e:
        logger.error("qhm_thesis: Slack send failed: %s", e)
    return 0


def _sector_of(sym: str) -> str:
    for sector, names in _BEYOND_SCAN.items():
        if sym in names:
            return sector
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
