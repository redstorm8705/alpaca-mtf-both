# ruff: noqa: E501  — long prompt strings and analysis text are intentionally long
"""
midday_audit.py
Post-market decision quality audit — runs once daily after RTH closes.
Reads today's trade_events.jsonl + last 4h of bot.log. Flags:
  - High-MRI entries (STRESSED / CRISIS level at entry)
  - Stop-hit rate and partial exit rate for the session
  - Error and warning counts from the live log
  - Watchdog restart events (hang indicator)
Posts a structured Slack summary. No execution imports. Read-only.

Schedule: 13:30 ET weekdays = 10:30 PT, MID-SESSION (OCI crontab via scripts/cron_tz_wrapper.py 13:30).
Stop coverage comes from reporting/broker_ground_truth.py (Alpaca order + fill history), never log text.
"""

import os
import sys
import json
import re
import ssl
import logging
import importlib
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from gai_client import GAI_MODEL_LADDER  # single source of truth for the live Gemini model ladder
from dotenv import load_dotenv  # must precede module-level os.getenv() calls (E402 fix)

load_dotenv()

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
_now = datetime.now(PT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("midday_audit")

# ── Config ───────────────────────────────────────────────────────────────────
SLACK_WEBHOOK  = os.getenv("SLACK_WEBHOOK_URL", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL   = GAI_MODEL_LADDER[0]   # primary + display; the full ladder drives the fallback loop
BASE_DIR       = Path(__file__).parent
LOGS_DIR       = BASE_DIR / "logs"
TRADE_EVENTS   = LOGS_DIR / "trade_events.jsonl"
BOT_LOG        = LOGS_DIR / "mtf_bot.log"
AUDIT_DATE     = _now.strftime("%Y-%m-%d")          # PT date
AUDIT_DATE_ET  = datetime.now(ET).strftime("%Y-%m-%d")  # ET date (for log matching)
REPORT_PATH    = LOGS_DIR / f"midday_audit_{AUDIT_DATE}.json"
GEMINI_REPORT  = LOGS_DIR / f"midday_gemini_{AUDIT_DATE}.txt"

# Risk thresholds
ATH_WARN_PCT    = 2.0   # flag entries taken within 2% of ATH
MRI_WARN_LEVELS = {"STRESSED", "CRISIS"}
STOP_HIT_WARN   = 0.5   # flag if >50% of today's entries hit their stop

# ── SSL context ───────────────────────────────────────────────────────────────
try:
    import certifi as _certifi
    _SSL_CTX = ssl.create_default_context(cafile=_certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()


# ─────────────────────────────────────────────────────────────────────────────
# Data readers
# ─────────────────────────────────────────────────────────────────────────────

def read_today_trade_events() -> list[dict]:
    """Read today's trade events plus prior-day entry events for overnight positions.

    Overnight positions have their 'entry' event in a prior day's records.
    Without those entries, analyse_pnl() sees exits with no matching entry and
    reports 0 closed trades / $0.00 P&L — the BUG-P&L-1 midday FAIL root cause.
    Fix: load all events once, take today's slice, then back-fill the most recent
    prior-day entry for any symbol that has a today-exit but no today-entry.
    """
    if not TRADE_EVENTS.exists():
        return []

    all_events: list[dict] = []
    with open(TRADE_EVENTS) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                all_events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    today_events = [
        e for e in all_events
        if e.get("ts", "").startswith(AUDIT_DATE) or e.get("ts", "").startswith(AUDIT_DATE_ET)
    ]

    # Symbols with exits today but no entry today → overnight hold; entry is prior-day
    today_exit_syms  = {e["symbol"] for e in today_events
                        if e.get("event") in ("exit", "stop_hit", "partial_exit")}
    today_entry_syms = {e["symbol"] for e in today_events if e.get("event") == "entry"}
    overnight_syms   = today_exit_syms - today_entry_syms

    if overnight_syms:
        # Walk all events chronologically; keep the last prior-day entry per symbol
        prior_entries: dict[str, dict] = {}
        for e in all_events:
            sym = e.get("symbol", "")
            if e.get("event") == "entry" and sym in overnight_syms:
                ts = e.get("ts", "")
                if not (ts.startswith(AUDIT_DATE) or ts.startswith(AUDIT_DATE_ET)):
                    prior_entries[sym] = e   # overwrite keeps the most recent prior entry
        if prior_entries:
            logger.info(
                f"Overnight positions detected — injecting {len(prior_entries)} prior-day "
                f"entry event(s) for P&L matching: {sorted(prior_entries)}"
            )
            today_events.extend(prior_entries.values())

    return today_events


def read_bot_log_tail(hours: int = 4) -> list[str]:
    """Return log lines from the last `hours` hours. Cap at 2000 lines."""
    if not BOT_LOG.exists():
        return []
    # mtf_bot.log timestamps are UTC (OCI host clock; verified 2026-09-24) — compare in UTC. The prior
    # PT cutoff against UTC stamps widened "the last 4h" to ~11h (pulled in the prior evening).
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    with open(BOT_LOG, errors="replace") as f:
        for line in f:
            lines.append(line.rstrip())
    # Filter to lines at or after cutoff (log timestamps are UTC, compared as strings)
    result = []
    for line in reversed(lines):
        ts_prefix = line[:19]
        if ts_prefix >= cutoff_str:
            result.append(line)
        if len(result) >= 2000:
            break
    return list(reversed(result))


# ─────────────────────────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyse_entries(events: list[dict]) -> dict:
    """Extract entry-level risk flags from today's trade events."""
    entries = [e for e in events if e.get("event") == "entry"]
    exits   = [e for e in events if e.get("event") in ("exit", "stop_hit")]
    partials = [e for e in events if e.get("event") == "partial_exit"]

    flags = []
    entry_symbols = []

    for e in entries:
        sym      = e.get("symbol", "?")
        score    = e.get("score", 0)
        mri_lvl  = e.get("mri_level", "NORMAL")
        price    = e.get("price", 0)
        ts       = e.get("ts", "")[:19].replace("T", " ")
        data_src = e.get("data_source", "unknown")

        entry_symbols.append(sym)
        entry_flags = []

        # High-stress MRI at entry
        if mri_lvl in MRI_WARN_LEVELS:
            entry_flags.append(f"MRI={mri_lvl} at entry")

        # Low-conviction entry
        if 0 < score < 10:
            entry_flags.append(f"low score ({score}/12)")

        if entry_flags:
            flags.append({
                "symbol": sym,
                "ts":     ts,
                "score":  score,
                "mri":    mri_lvl,
                "price":  price,
                "source": data_src,
                "flags":  entry_flags,
            })

    # Stop-hit rate
    stop_hits = [e for e in events if e.get("event") == "stop_hit"]
    stop_hit_rate = len(stop_hits) / len(entries) if entries else 0.0

    return {
        "entry_count":    len(entries),
        "exit_count":     len(exits),
        "partial_count":  len(partials),
        "stop_hit_count": len(stop_hits),
        "stop_hit_rate":  stop_hit_rate,
        "flagged_entries": flags,
        "entry_symbols":  entry_symbols,
    }


def analyse_pnl(events: list[dict]) -> dict:
    """
    Compute session P&L metrics from matched entry/exit pairs in trade_events.jsonl.
    Matches on symbol (one trade per symbol per session — enforced by sector gate).
    Returns wins, losses, total_pnl, win_rate, profit_factor, best/worst trade.
    """
    # Sorted by timestamp so chronological pair matching works for multiple trades/symbol/day.
    _raw_entries = sorted(
        [e for e in events if e.get("event") == "entry"],
        key=lambda e: e.get("ts", ""),
    )
    _raw_exits = sorted(
        [e for e in events if e.get("event") in ("exit", "stop_hit", "partial_exit")],
        key=lambda e: e.get("ts", ""),
    )
    # Match each entry to the next chronological exit for the same symbol.
    _exit_queue: dict[str, list[dict]] = {}
    for ex in _raw_exits:
        _exit_queue.setdefault(ex["symbol"], []).append(ex)

    trades = []
    for en in _raw_entries:
        sym = en["symbol"]
        ex_list = _exit_queue.get(sym, [])
        if not ex_list:
            continue
        ex = ex_list.pop(0)
        ep  = float(en.get("price") or 0)
        xp  = float(ex.get("price") or 0)
        qty = float((ex.get("size") if ex.get("event") == "partial_exit" else en.get("size")) or 0)
        if ep <= 0 or qty <= 0:
            continue
        direction = en.get("direction", "long")
        raw_pnl   = (xp - ep) * qty if direction != "short" else (ep - xp) * qty
        trades.append({
            "symbol":      sym,
            "pnl":         raw_pnl,
            "entry_price": ep,
            "exit_price":  xp,
            "size":        qty,
            "exit_event":  ex.get("event", "exit"),
            "score":       int(en.get("score") or 0),
            "mri_level":   en.get("mri_level", "NORMAL"),
            "stop":        float(en.get("stop")   or 0),
            "target":      float(en.get("target") or 0),
        })

    if not trades:
        return {"count": 0, "wins": 0, "losses": 0, "total_pnl": 0.0,
                "win_rate": 0.0, "profit_factor": None,
                "avg_win": 0.0, "avg_loss": 0.0,
                "best": None, "worst": None, "trades": []}

    wins    = [t for t in trades if t["pnl"] > 0]
    losses  = [t for t in trades if t["pnl"] <= 0]
    total   = sum(t["pnl"] for t in trades)
    avg_win = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_los = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0
    pf      = abs(avg_win / avg_los) if avg_los != 0 else None
    best    = max(trades, key=lambda t: t["pnl"])
    worst   = min(trades, key=lambda t: t["pnl"])

    return {
        "count":         len(trades),
        "wins":          len(wins),
        "losses":        len(losses),
        "total_pnl":     total,
        "win_rate":      len(wins) / len(trades),
        "profit_factor": pf,
        "avg_win":       avg_win,
        "avg_loss":      avg_los,
        "best":          best,
        "worst":         worst,
        "trades":        trades,
    }


def run_signal_postmortem(events: list[dict]) -> list[dict]:
    """
    For every entry+exit pair closed today, write a signal record and call
    postmortem_recorder.py so outcomes feed back into skill weight calibration.
    Returns list of postmortem result dicts (one per closed trade).
    """
    import subprocess
    POSTMORTEM_SCRIPT = BASE_DIR / "claude-trading-skills-main" / "skills" / "signal-postmortem" / "scripts" / "postmortem_recorder.py"
    FMP_KEY = os.getenv("FMP_API_KEY", "").strip()
    STATE_DIR = BASE_DIR / "data" / "state" / "postmortem"
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if not POSTMORTEM_SCRIPT.exists():
        logger.warning("postmortem_recorder.py not found — skipping postmortem.")
        return []

    _pm_entries = sorted(
        [e for e in events if e.get("event") == "entry"],
        key=lambda e: e.get("ts", ""),
    )
    _pm_exits = sorted(
        [e for e in events if e.get("event") in ("exit", "stop_hit")],
        key=lambda e: e.get("ts", ""),
    )
    _pm_exit_q: dict[str, list] = {}
    for _ex in _pm_exits:
        _pm_exit_q.setdefault(_ex["symbol"], []).append(_ex)
    entries = {en["symbol"]: en for en in _pm_entries}  # last entry per sym for postmortem
    exits   = {sym: q[0] for sym, q in _pm_exit_q.items() if q}

    closed = set(entries) & set(exits)
    if not closed:
        logger.info("No closed trades today — postmortem skipped.")
        return []

    results = []
    for sym in sorted(closed):
        entry_ev = entries[sym]
        exit_ev  = exits[sym]
        signal_id = f"{AUDIT_DATE}_{sym}"
        signal_file = STATE_DIR / f"{signal_id}.json"

        signal_record = {
            "signal_id":          signal_id,
            "ticker":             sym,
            "signal_date":        AUDIT_DATE,
            "predicted_direction": "LONG" if entry_ev.get("direction", "long") == "long" else "SHORT",
            "source_skill":       "mtf_confluence_bot",
            "entry_price":        float(entry_ev.get("price", 0)),
            "exit_price":         float(exit_ev.get("price", 0)),
            "entry_score":        int(entry_ev.get("score", 0)),
            "pdt_at_entry":       int(entry_ev.get("pdt_used", 0)),
            "mri_at_entry":       entry_ev.get("mri_level", "NORMAL"),
            "exit_reason":        exit_ev.get("reason", exit_ev.get("event", "unknown")),
        }
        with open(signal_file, "w") as f:
            json.dump(signal_record, f, indent=2)

        cmd = [
            sys.executable, str(POSTMORTEM_SCRIPT),
            "--signal-file", str(signal_file),
            "--exit-price",  str(signal_record["exit_price"]),
            "--exit-date",   AUDIT_DATE,
        ]
        if FMP_KEY:
            cmd += ["--api-key", FMP_KEY]

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            outcome = "OK" if res.returncode == 0 else f"rc={res.returncode}"
            logger.info(f"  Postmortem {sym}: {outcome}")
            results.append({"symbol": sym, "status": outcome,
                             "entry": signal_record["entry_price"],
                             "exit":  signal_record["exit_price"]})
        except Exception as e:
            logger.warning(f"  Postmortem {sym} failed: {e}")
            results.append({"symbol": sym, "status": f"error: {e}"})

    return results


def analyse_mri(events: list[dict]) -> dict:
    """Summarise MRI level distribution across the session."""
    mri_events = [e for e in events if e.get("event") == "mri_refresh"]
    if not mri_events:
        return {"samples": 0, "levels": {}}
    level_counts: dict[str, int] = {}
    for e in mri_events:
        lvl = e.get("mri_level", "UNKNOWN")
        level_counts[lvl] = level_counts.get(lvl, 0) + 1
    last = mri_events[-1]
    return {
        "samples":    len(mri_events),
        "levels":     level_counts,
        "last_score": last.get("score", 0),
        "last_level": last.get("mri_level", "UNKNOWN"),
    }


def analyse_log(log_lines: list[str]) -> dict:
    """Count errors, warnings, watchdog events from the last 4h of bot.log."""
    errors   = []
    warnings = []
    watchdog_events = []
    NOISE = ("Finnhub", "TruthSocial", "Truth Social", "news_monitor",
             "Shorting:", "too many requests", "not allowed to short",
             "position size < 1", "PDT protection", "already has Alpaca",
             "scan_breaking_news: source timeout")

    for line in log_lines:
        if " ERROR " in line:
            errors.append(line.strip()[:150])
        elif " WARNING " in line and not any(n in line for n in NOISE):
            warnings.append(line.strip()[:150])
        if "WATCHDOG" in line or "BOT HUNG" in line or "AUTO-RESTART" in line.upper():
            watchdog_events.append(line.strip()[:150])

    return {
        "error_count":    len(errors),
        "warning_count":  len(warnings),
        "watchdog_count": len(watchdog_events),
        "error_samples":  errors[:5],
        "warning_samples": warnings[:5],
        "watchdog_lines": watchdog_events[:3],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Slack post
# ─────────────────────────────────────────────────────────────────────────────

def _slack(title: str, body: str, emoji: str = ":bar_chart:") -> None:
    if not SLACK_WEBHOOK:
        logger.warning("No SLACK_WEBHOOK_URL — printing to stdout instead.")
        print(f"{emoji} *{title}*\n{body}")
        return
    payload = json.dumps({"text": f"{emoji} *{title}*\n{body}"}).encode()
    req = urllib.request.Request(
        SLACK_WEBHOOK,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as resp:
            if resp.status not in (200, 204):
                logger.warning(f"Slack returned {resp.status}")
    except Exception as e:
        logger.warning(f"Slack post failed: {e}")


def build_slack_body(entry_analysis: dict, mri_analysis: dict, log_analysis: dict,
                     pnl_analysis: "dict | None" = None,
                     postmortem: "list | None" = None) -> str:
    lines = []
    pnl = pnl_analysis or {}

    # ── 1. P&L Performance (lead section) ────────────────────────────────────
    n_closed = pnl.get("count", 0)
    if n_closed > 0:
        pnl_sign  = "+" if pnl["total_pnl"] >= 0 else ""
        wr_pct    = f"{pnl['win_rate']:.0%}"
        pf_str    = f"{pnl['profit_factor']:.2f}" if pnl["profit_factor"] is not None else "∞"
        best      = pnl.get("best")
        worst     = pnl.get("worst")
        lines.append(
            f"*P&L — {AUDIT_DATE} PT:* "
            f"{pnl_sign}${pnl['total_pnl']:.2f} realized | "
            f"{pnl['wins']}W / {pnl['losses']}L ({wr_pct} WR) | "
            f"PF {pf_str} | "
            f"Avg W ${pnl['avg_win']:.2f} / Avg L ${pnl['avg_loss']:.2f}"
        )
        if best and worst:
            lines.append(
                f"  Best: {best['symbol']} +${best['pnl']:.2f} "
                f"(score {best['score']}/12, MRI={best['mri_level']}) | "
                f"Worst: {worst['symbol']} ${worst['pnl']:.2f} "
                f"(score {worst['score']}/12, {worst['exit_event']})"
            )
        # Score vs outcome: group closed trades by score bracket
        by_score: dict[int, list] = {}
        for t in pnl.get("trades", []):
            by_score.setdefault(t["score"], []).append(t["pnl"])
        if len(by_score) > 1:
            score_summary = " | ".join(
                f"{s}/12: {sum(1 for p in ps if p>0)}/{len(ps)}W "
                f"(${sum(ps):+.2f})"
                for s, ps in sorted(by_score.items(), reverse=True)
            )
            lines.append(f"  Score→Outcome: {score_summary}")
    else:
        lines.append(f"*P&L — {AUDIT_DATE} PT:* no closed trades this session")

    # ── 2. Execution Quality ──────────────────────────────────────────────────
    lines.append(
        f"*Execution:* "
        f"{entry_analysis['entry_count']} entries | "
        f"{entry_analysis['exit_count']} exits | "
        f"{entry_analysis['partial_count']} partials | "
        f"stop-hit rate {entry_analysis['stop_hit_rate']:.0%}"
    )
    if entry_analysis["stop_hit_rate"] >= STOP_HIT_WARN and entry_analysis["entry_count"] > 0:
        lines.append(
            f"  ⛔ Stop rate ≥{STOP_HIT_WARN:.0%} — "
            f"strategy thesis misaligned with session conditions. "
            f"Review MIN_SCORE or regime gating before tomorrow."
        )

    # ── 3. Regime & Strategy Context ─────────────────────────────────────────
    mri_dist = " / ".join(
        f"{lvl}×{cnt}" for lvl, cnt in sorted(mri_analysis.get("levels", {}).items())
    )
    lines.append(
        f"*Regime:* MRI close={mri_analysis.get('last_level','?')} "
        f"({mri_analysis.get('last_score','?')}/100) | "
        f"Session distribution: {mri_dist or 'no data'}"
    )

    # ── 4. Flagged Entries ────────────────────────────────────────────────────
    flagged = entry_analysis["flagged_entries"]
    if flagged:
        lines.append(f"*⚠️ Flagged Entries ({len(flagged)}):*")
        for fe in flagged:
            flag_str = ", ".join(fe["flags"])
            lines.append(
                f"  • {fe['symbol']} @ {fe['ts']} PT — "
                f"score {fe['score']}/12 | "
                f"MRI={fe['mri']} | {flag_str}"
            )
    else:
        lines.append("*Flagged Entries:* none — all entries within parameters")

    # ── 5. Signal Postmortem (if available) ───────────────────────────────────
    if postmortem:
        lines.append(f"*Signal Postmortem ({len(postmortem)} closed trades):*")
        for pm in postmortem:
            ret = ""
            if pm.get("entry") and pm.get("exit"):
                pct = (pm["exit"] - pm["entry"]) / pm["entry"] * 100
                ret = f" {pct:+.2f}%"
            lines.append(f"  • {pm['symbol']}{ret} — {pm['status']}")

    # ── 6. Infrastructure (condensed — bottom) ────────────────────────────────
    infra_parts = [
        f"{log_analysis['error_count']} errors",
        f"{log_analysis['warning_count']} warnings",
    ]
    if log_analysis["watchdog_count"] > 0:
        infra_parts.append(f"🔴 {log_analysis['watchdog_count']} watchdog restart(s)")
    lines.append(f"*Infra (last 4h):* {' | '.join(infra_parts)}")
    if log_analysis["watchdog_count"] > 0:
        for wl in log_analysis["watchdog_lines"]:
            lines.append(f"  `{wl[:120]}`")
    if log_analysis["error_samples"]:
        for e in log_analysis["error_samples"][:3]:
            lines.append(f"  `{e[:120]}`")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Gemini integration
# ─────────────────────────────────────────────────────────────────────────────

def _build_config_constants_block() -> str:
    """Resolve live config values at audit-build time instead of hand-typing a
    snapshot. S68 fix (2026-06-27, board+Gro+GAI) — see nightly_audit.py's
    identical helper for the full incident writeup (stale PDT framing, wrong
    constant names/values silently drifting for months). Duplicated here
    rather than imported since these two scripts are independently standalone
    by design (each can run with only its own file present)."""
    try:
        sys.path.insert(0, str(BASE_DIR))
        import config as _live_cfg
        importlib.reload(_live_cfg)
    except Exception as e:
        return f"(config.py import failed: {e} — constants below are UNVERIFIED, do not audit against them)"

    profile = _live_cfg.PROFILES.get("paper", {}) if hasattr(_live_cfg, "PROFILES") else {}

    def _resolve(name: str) -> str:
        if name in profile:
            return f"{name} = {profile[name]!r}  (paper profile)"
        if hasattr(_live_cfg, name):
            return f"{name} = {getattr(_live_cfg, name)!r}  (module-level default)"
        return f"{name} = NOT FOUND in config.py — do not assert a value for this"

    names = [
        "MIN_LONG_SCORE", "MIN_SHORT_SCORE", "KELLY_FRACTION", "MAX_OPEN_POSITIONS",
        "BUCKET_B_MAX_POSITIONS_POWER",
        "INTRADAY_STOP_ATR_MULT", "INTRADAY_TARGET_ATR_MULT", "MAX_DAILY_LOSS_PCT",
        "VIX_BE_WIDEN_THRESHOLD_1", "VIX_BE_WIDEN_THRESHOLD_2",
        "VIX_STOP_WIDEN_THRESHOLD_1", "VIX_STOP_WIDEN_THRESHOLD_2",
        "VIX_STOP_WIDEN_MULT_1", "VIX_STOP_WIDEN_MULT_2",
        "VOLATILITY_TIER_HIGH_THRESHOLD", "VOLATILITY_TIER_EXTREME_THRESHOLD",
        "VOL_TIER_STD_STOP_INTRADAY", "VOL_TIER_HIGH_STOP_INTRADAY",
        "VOL_TIER_EXTREME_STOP_INTRADAY",
        "OVERNIGHT_ENTRIES_ENABLED", "LEVERAGED_NOTIONAL_MAX_PCT",
        "TOD_MARKET_OPEN_BUFFER_MINS", "TOD_EOD_NO_ENTRY_MINS",
    ]
    return "\n".join(_resolve(n) for n in names)


def _build_gemini_prompt(entry_anal: dict, mri_anal: dict, log_anal: dict,
                         pnl_anal: dict, log_lines: list[str],
                         gt_block: str, fills_sum: dict) -> str:
    # Stop coverage = broker ground truth computed by code (reporting/broker_ground_truth.py).
    stop_str = gt_block or ("BROKER GROUND TRUTH: unavailable — stop coverage UNVERIFIED: report "
                            "UNKNOWN; a naked position may be reported only when the bot's own log "
                            "states it, quoted and tagged broker-unverified.")

    # Alpaca fills — ground truth (entry-EVENT log is degraded, D1).
    if not fills_sum.get("available", False):
        fills_str = "(Alpaca fills unavailable — could not fetch; do not infer trade activity from event counts.)"
    else:
        _bysym = ", ".join(f"{s}: {d['buy']}B/{d['sell']}S"
                           for s, d in sorted(fills_sum.get("by_symbol", {}).items()))
        fills_str = (f"{fills_sum.get('n_fills', 0)} fills across "
                     f"{len(fills_sum.get('symbols', []))} symbol(s) — {_bysym or 'none'}")

    flagged_str = ""
    for fe in entry_anal.get("flagged_entries", []):
        flagged_str += (
            f"  - {fe['symbol']} @ {fe['ts']} PT | score {fe['score']}/12 | "
            f"MRI={fe['mri']} | flags: {', '.join(fe['flags'])}\n"
        )
    if not flagged_str:
        flagged_str = "  None\n"

    trades_str = ""
    for t in pnl_anal.get("trades", []):
        pnl_sign = "+" if t["pnl"] >= 0 else ""
        _stop   = t.get("stop",   0)
        _tgt    = t.get("target", 0)
        _ep     = t["entry_price"]
        _t1     = round(_ep + 0.20 * (_tgt - _ep), 2) if _tgt else 0
        _t2     = round(_ep + 0.40 * (_tgt - _ep), 2) if _tgt else 0
        _lvls   = (f"stop=${_stop:.2f} | T1=${_t1:.2f} T2=${_t2:.2f} T3=${_tgt:.2f}"
                   if _stop and _tgt else "levels=unknown")
        trades_str += (
            f"  {t['symbol']}: entry ${_ep:.2f} → exit ${t['exit_price']:.2f} "
            f"({pnl_sign}${t['pnl']:.2f}) | score {t['score']}/12 | MRI={t['mri_level']} | "
            f"{t['exit_event']} | {_lvls}\n"
        )
    if not trades_str:
        trades_str = "  No closed trades\n"

    log_sample = "\n".join(log_lines[-200:]) if log_lines else "(no log data)"

    return f"""You are an adversarial intraday decision quality auditor for a live Alpaca
paper-trading bot (MTF confluence scoring, $2.5K paper account).
Your job is to find problems — bad entries, bad exits, logic errors, and anything
that indicates systematic degradation in decision quality.

## VERIFICATION DISCIPLINE — MANDATORY, READ FIRST

1. Before flagging any bug: (1) state the exact variable or metric involved, (2) explain
   what the value is vs. what it should be and why that constitutes a failure, (3) state
   the exact condition under which this occurs. Do NOT flag something as a bug if you
   cannot complete step (2). Do not hallucinate code not shown to you — if source is
   not provided, say "source not provided."
2. Do not invent function names, state/enum names, or variable names not shown in the
   data below. If you cannot find verbatim evidence for a claim, say "not found in
   provided source" — do not guess a plausible-sounding name.
3. When citing any numeric field for a specific trade (price, stop, score, P&L), pull
   every field for that citation from the SAME entry in CLOSED TRADE DETAILS or FLAGGED
   ENTRIES below — never combine fields from two different trades for the same symbol.
4. Check CONFIG CONSTANTS below before flagging any violation — these are resolved
   live from config.py at report-build time, not a hand-typed snapshot. If a constant
   shows "NOT FOUND," do not assert any value for it.
5. If you disagree with a prior audit finding or a proposed fix: (a) trace the exact
   code path that disproves the finding, (b) cite the relevant CONFIG CONSTANT with
   its actual resolved value, (c) provide an alternative fix with file name and line
   context, (d) flag it as "METHODOLOGY DISAGREEMENT" — not as a new bug.

## ARCHITECTURE INVARIANT — PDT DOES NOT EXIST

Pattern Day Trader (PDT) enforcement was PERMANENTLY DELETED from this codebase
(SEC rule change, S63 sweep). There is zero PDT logic anywhere in execution code.
NEVER attribute any halt, block, or anomaly to PDT — find the actual cause in the
provided data instead.

Separate all findings by category:
  EXECUTION BUG — code logic that produces wrong runtime behavior (wrong price recorded, wrong exit triggered)
  ALPHA ISSUE   — strategy quality (entries in bad conditions, exits mistimed vs. stated TP/SL)
  INFRASTRUCTURE — memory, network, watchdog, API quirks

---

## AUDIT DATE: {AUDIT_DATE} PT

---

## SESSION P&L SUMMARY
Entries: {entry_anal['entry_count']} | Exits: {entry_anal['exit_count']} | Partials: {entry_anal['partial_count']}
Stop-hit rate: {entry_anal['stop_hit_rate']:.0%} ({entry_anal['stop_hit_count']} stops)
Closed trades: {pnl_anal.get('count', 0)} | Wins: {pnl_anal.get('wins', 0)} | Losses: {pnl_anal.get('losses', 0)}
Total P&L: ${pnl_anal.get('total_pnl', 0):.2f} | Win rate: {pnl_anal.get('win_rate', 0):.0%}
Avg win: ${pnl_anal.get('avg_win', 0):.2f} | Avg loss: ${pnl_anal.get('avg_loss', 0):.2f}

---

## LIVE STOP COVERAGE — BROKER GROUND TRUTH (computed by code from Alpaca order + fill history)
This is the ONLY valid source for whether a position is protected. Never infer "naked" / "no stop" from
log text. Classes: COVERED (a broker stop covers the full qty); SOFTWARE-ONLY-BY-DESIGN (core intraday
position protected by the bot's software stop — by design until the pre-close sweep at ~12:45-12:52 PT,
which has NOT run yet at this midday check, so this is LIVE software-only exposure); BROKER-STOP-LAPSED;
SOFTWARE-ONLY+CYCLE-GAP (bot loop stalled); NAKED (nothing protecting it); UNKNOWN (unverified).
STOP COVERAGE IS OWNED BY CODE: every alarm below is already posted on the card by code. Do NOT write a
naked / no-stop claim for a symbol classed COVERED or SOFTWARE-ONLY-BY-DESIGN, and do not repeat NAKED /
LAPSED / CYCLE-GAP symbols. EXCEPTION (live exposure mid-session): a bot self-report of a stop or exit
FAILURE on a symbol still held (e.g. "unprotected", "FAILED", "Set manual stop") DOES belong in
CATASTROPHIC ALERT — quote the exact log line. If the block is UNKNOWN, write "stop coverage UNKNOWN"
under LOG ANOMALIES; a naked position may be reported only when the bot's own log states it, quoted
and tagged broker-unverified.
```
{stop_str}
```

## ALPACA FILLS TODAY — GROUND TRUTH (use INSTEAD of entry-event counts)
{fills_str}

CONTEXT — READ BEFORE JUDGING TRADE COUNTS: The bot's `entry`-EVENT log (trade_events.jsonl) is
currently DEGRADED — it may be missing `entry` records (known issue D1, fix in flight). The
SESSION P&L SUMMARY and CLOSED TRADE DETAILS above are computed by matching entry↔exit EVENTS, so
when entry events are missing they UNDER-count closed trades and can show $0 matched P&L even though
the bot DID trade. This is the KNOWN D1 symptom — do NOT flag "0 entries / $0 P&L while exits exist"
(or exits without matching entries) as a trade-accounting failure or P&L corruption. Use ALPACA
FILLS above as the ground truth for what the bot actually traded; the authoritative same-day P&L is
the ledger / live mark-to-market, NOT this matched-pair engine. (The genuinely CATASTROPHIC P&L
condition is a position LIVE STOP COVERAGE classes NAKED, or a quoted bot self-report of a stop/exit
failure — not a matched-pair under-count.)

---

## CLOSED TRADE DETAILS
{trades_str}
---

## FLAGGED ENTRIES
{flagged_str}
---

## MRI SESSION DISTRIBUTION
Samples: {mri_anal.get('samples', 0)} | Close level: {mri_anal.get('last_level', '?')} ({mri_anal.get('last_score', '?')}/100)
Level distribution: {mri_anal.get('levels', {})}

---

## BOT CONFIG CONSTANTS (resolved live from config.py at report-build time — verify against these before flagging a violation; "NOT FOUND" means do not assert a value)
{_build_config_constants_block()}

---

## KNOWN BENIGN PATTERNS (do NOT flag these)
- "EOD summary written" or "Alpaca FIFO EOD" every ~5 min: crash-safety flushes — intentional. Flag only if gap > 20 min.
- "A-4 paper fills gap: Alpaca returned 0 fills": expected Alpaca paper account behavior. Bot falls back to tracker P&L correctly.
- 5 simultaneous open positions during 9:35–10:00 AM ET: BUCKET_B_MAX_POSITIONS_POWER=5 — intentional power-hour expansion. NOT a max-positions violation.
- Error 42210000 from GTC stop submission in extended hours (pre-RTH / AH): known open bug OM-BUG-1, tracked. Do NOT flag pre-RTH/AH occurrences. DO flag if 42210000 occurs during RTH (9:30–16:00 ET).
- Exit P&L = $0.00 on overnight_breakeven exit reason: valid if stop_price was pushed to entry_price. NOT a recording bug.

---

## BOT LOG (last 4h — WARNING/ERROR/key events)
CONTEXT: Lines matching "EOD summary written" or "Alpaca FIFO EOD" appearing every ~5 minutes
are PERIODIC CRASH-SAFETY FLUSHES — intentional design, not a logic error. Flag only if
spacing > 20 minutes (missed flush) or < 1 minute (runaway loop). Do NOT flag them as
"premature EOD processing" or "repeated EOD cycles."

CONTEXT: "A-4 paper fills gap: Alpaca returned 0 fills" is EXPECTED behavior on Alpaca paper
accounts — same-day fills are not available until next-day settlement. The bot correctly falls
back to tracker P&L. This is not a trade accounting failure.

CONTEXT: 5 concurrent open positions during 9:35–10:00 AM ET = power-hour expansion
(BUCKET_B_MAX_POSITIONS_POWER=5). This is intentional config, NOT a max-positions violation.
```
{log_sample}
```

---

## YOUR TASKS

1. **Performance audit — entry/exit/P&L**:
   - Are entry prices plausible for the symbol and time of day?
   - Are exit prices consistent with stated exit reason (stop hit → near stop price, TP → near target)?
   - Are P&L signs correct (long: exit > entry = profit; short: entry > exit = profit)?
   - Is win rate and profit factor consistent with the bot's edge claims?
   - Is there score→outcome correlation? (Higher score entries should win more often.)
   - Does stop-hit rate indicate the strategy thesis is misaligned with current regime?
   - Flag any trade where P&L = 0.0 but entry ≠ exit price (recording bug).
   - Flag any partial exit that doesn't reduce position size correctly.

2. **Entry quality**: Were entries taken under conditions that systematically reduce edge?
   (High MRI, low score, thin volume, bad timing)

3. **Exit quality**: Did the bot hold winners too long or cut losers too slowly?
   Compare exit reason vs. actual price vs. stated TP/SL levels.

4. **MRI alignment**: Were entries taken at STRESSED/CRISIS MRI levels? Was position sizing
   adjusted appropriately? Any regime mismatch?

5. **Log anomalies**: Flag recurring ERRORs, WARNING patterns, watchdog events, stale data,
   rate-limit hits, or anything that suggests infrastructure degradation.

6. **New bugs**: Any issues you spot in the data that weren't in the flagged entries list.
   Check KNOWN BENIGN PATTERNS above first. Do NOT flag API timeouts on paper accounts.
   Do NOT flag 5-position counts during power hour. For each finding: state exact failure
   condition, variable involved, and why it produces wrong output. Tag each as
   EXECUTION BUG, ALPHA ISSUE, or INFRASTRUCTURE.

---

## OUTPUT FORMAT

Respond in this exact structure. CATASTROPHIC ALERT comes FIRST, before
anything else, so it cannot be missed — this report feeds an unattended
overnight review pipeline.

### CATASTROPHIC ALERT: [count]
List ONLY items meeting this bar, or write "None — no catastrophic conditions
detected." One line each: exact failure condition | impacted symbol/trade.
  CATASTROPHIC = a naked position per the LIVE STOP COVERAGE rules above (never
  inferred from log text), a quoted bot self-report of a stop/exit failure on a
  held symbol, silent trading halt, P&L
  corruption affecting live risk decisions, kill switch triggered but not
  respected.

### VERDICT: [PASS | WARN | FAIL]
(PASS = session clean; WARN = issues found but manageable; FAIL = CATASTROPHIC count > 0, OR data corruption, OR systematic failure)

### PERFORMANCE AUDIT
(entry/exit integrity, P&L sign checks, score→outcome correlation, stop rate analysis)

### ENTRY QUALITY
(findings or "No issues")

### EXIT QUALITY
(findings or "No issues")

### MRI ALIGNMENT
(findings or "No issues")

### LOG ANOMALIES
(bullet list or "None detected")

### NEW BUGS / CONCERNS
For each: CATEGORY | SEVERITY | description | exact condition
CATEGORY: EXECUTION BUG | ALPHA ISSUE | INFRASTRUCTURE
SEVERITY: CRITICAL | HIGH | MEDIUM | LOW
  (CATASTROPHIC-tier items belong in CATASTROPHIC ALERT above — do not
   duplicate them here.)
  CRITICAL = silent trading halt risk, P&L corruption in a non-live-risk path
  HIGH = incorrect fill price, failed stop submission, wrong exit triggered
  MEDIUM = missing cleanup/reset, edge-case state error, logging gap
  LOW = cosmetic logging issue, minor redundancy
(or "None")

### FIX VALIDATION
For any bug you flag for which a fix was proposed (in a prior session or report):
  FIX VALIDATION: PASS / FAIL / PARTIAL
  If FAIL: exact reason (e.g., "fix causes watchdog restart when X occurs")
  Alternative fix: file name + code pattern
(or "No prior fixes to validate")

### RECOMMENDED ACTIONS
(ordered by urgency — max 3 items; tag each as EXECUTION, ALPHA, or INFRA;
if CATASTROPHIC count > 0, item #1 must address it)
"""


def _call_gemini(prompt: str) -> str:
    """Send prompt to Gemini Flash, return text. Falls back through model tiers."""
    # Shared laddered client — the SINGLE source of truth for the model list (gai_client.GAI_MODEL_LADDER)
    # AND thinking_budget=0. Replaces the old [GEMINI_MODEL, dead gemini-2.0-flash-lite] SDK loop, which
    # (a) pinned a retired fallback and (b) set NO thinking_budget=0 — so a thinking model returning
    # empty text was swallowed by `return response.text or ""` instead of laddering on. call_gai sets
    # thinking_budget=0 and treats an empty/unparseable response as skip-to-next-model.
    from gai_client import GAIError, call_gai
    try:
        return call_gai(prompt, GEMINI_API_KEY, max_output_tokens=8192)
    except GAIError as e:
        logger.error(f"All Gemini models exhausted: {e}")
        return f"(Gemini API error — all models failed: {e})"


def _extract_verdict(report: str) -> str:
    for line in report.splitlines():
        if "VERDICT:" in line:
            if "FAIL" in line:
                return "FAIL"
            if "WARN" in line:
                return "WARN"
            if "PASS" in line:
                return "PASS"
    return "UNKNOWN"


def _fetch_live_positions() -> "list | None":
    """Live open positions via Alpaca REST (T1 account data, raw urllib — this
    script is read-only by design and must not import execution/). The card's
    midday P&L is UNREALIZED mark-to-market from these positions (design lock
    2026-07-02): same-day realized numbers from trade events are unreliable on
    paper accounts until overnight settlement. None on failure → caller falls
    back to the legacy text summary."""
    key = os.getenv("ALPACA_API_KEY", "").strip()
    sec = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not key or not sec:
        return None
    req = urllib.request.Request(
        "https://paper-api.alpaca.markets/v2/positions",
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())
            return data if isinstance(data, list) else None
    except Exception as e:
        logger.warning(f"Live positions fetch failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Live stop-coverage & fill-activity — the one thing only the post-close midday
# run can do: verify live positions are actually protected NOW, and read real
# trade activity from Alpaca fills instead of the DEGRADED entry-event log (D1).
# All read-only raw urllib (no execution import). Field names verified live
# 2026-07-27 (orders: side/order_type/stop_price; fills: symbol/side/qty/price).
# ─────────────────────────────────────────────────────────────────────────────
_STOP_ORDER_TYPES = {"stop", "stop_limit", "trailing_stop"}


def _alpaca_get(path: str) -> "list | None":
    """GET an Alpaca paper REST endpoint, return a list or None on any failure.
    None → caller fails SAFE (never a false all-clear)."""
    key = os.getenv("ALPACA_API_KEY", "").strip()
    sec = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not key or not sec:
        return None
    req = urllib.request.Request(
        f"https://paper-api.alpaca.markets{path}",
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())
            return data if isinstance(data, list) else None
    except Exception as e:
        logger.warning(f"Alpaca GET {path} failed: {e}")
        return None


def _fetch_open_orders_nested() -> "list | None":
    """Open orders WITH their legs (nested=true) — day-tier OCO stops are legs of a limit parent;
    the prior un-nested read never saw them (false "NAKED AMZN" 09-23, "NAKED GOOGL" 09-24).
    Used only for the degraded fallback when the broker ground truth is UNKNOWN."""
    return _alpaca_get("/v2/orders?status=open&limit=500&nested=true")


def _fetch_today_fills() -> "list | None":
    """Today's FILL activities (ET date) — ground-truth trade activity, used
    instead of the degraded entry-EVENT log. None on failure."""
    return _alpaca_get(f"/v2/account/activities/FILL?date={AUDIT_DATE_ET}")


def snapshot_uncovered(positions: "list | None", orders: "list | None") -> "list | None":
    """DEGRADED fallback (ground truth UNKNOWN): symbols held right now with no same-side resting stop
    covering the full qty, counting OCO/bracket legs. Cannot tell core-by-design from day-tier, so the
    caller reports these as HIGH (never critical, never cleared). None if either read failed."""
    if positions is None or orders is None:
        return None
    flat: list = []
    for o in orders:
        flat.append(o)
        flat.extend(o.get("legs") or [])
    out = []
    for p in positions:
        sym = p.get("symbol", "?")
        try:
            qty = abs(float(p.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0.0
        need = "sell" if p.get("side", "long") == "long" else "buy"
        held = 0.0
        for o in flat:
            if (o.get("symbol") == sym and o.get("side") == need
                    and (o.get("type") or o.get("order_type")) in _STOP_ORDER_TYPES):
                try:
                    held += abs(float(o.get("qty") or 0))
                except (TypeError, ValueError):
                    continue
        if qty > 0 and held + 1e-9 < qty:
            out.append({"symbol": sym, "side": p.get("side", "long"), "qty": qty, "stop_qty": held})
    return out


GT_RETRIES = 3            # mid-session the bot trades every ~5 min: a fill racing the snapshot is likely
GT_RETRY_SLEEP_S = 30


def collect_ground_truth() -> dict:
    """broker_ground_truth.collect(today ET, now) with retries on UNKNOWN (e.g. "fills changed during
    the snapshot"). Never raises: returns the last UNKNOWN result if every attempt fails."""
    import time as _time
    gt: dict = {"status": "UNKNOWN", "reason": "not collected", "positions": {}}
    try:
        from reporting import broker_ground_truth as _bgt
    except Exception as e:
        return {"status": "UNKNOWN", "reason": f"module import failed: {e}", "positions": {}}
    for attempt in range(1, GT_RETRIES + 1):
        now = datetime.now(ET)
        gt = _bgt.collect(now.strftime("%Y-%m-%d"), now=now)
        if gt.get("status") != "UNKNOWN":
            return gt
        logger.warning("broker ground truth UNKNOWN (attempt %d/%d): %s", attempt, GT_RETRIES,
                       gt.get("reason"))
        if attempt < GT_RETRIES:
            _time.sleep(GT_RETRY_SLEEP_S)
    return gt


TRADE_LOG = BASE_DIR / "trade_log.json"
# A bot SELF-REPORT of a stop/exit failure: WARNING/ERROR/CRITICAL level, a stop/exit/close/protection word
# AND a failure word (board masked-loss seat: plain "failed" also matched entry-side lines such as
# "Short skipped — live shorting pre-flight failed", which is not a stop failure on a held position).
_LEVEL_RE = re.compile(r"\|\s*(WARNING|ERROR|CRITICAL)\s*\|")
_STOPWORD_RE = re.compile(r"stop|exit|close|protect", re.I)
_FAILWORD_RE = re.compile(r"fail|unprotected|naked", re.I)
# Entry-side lines that happen to contain both word classes ("… entry blocked (fail-closed)",
# "PRICE SANITY FAIL vs prior close — skipping entry") are not stop/exit failures (adversarial N1).
# NOT "skipping entry" alone: "#12c exit order submission failed — skipping entry" IS a failed exit
# on a still-held position (cold-2nd r6).
_NOT_EXIT_RE = re.compile(r"fail-closed|entry blocked|price sanity", re.I)


def _read_open_trades() -> "dict | None":
    """{symbol: open-trade record} from the core tracker's trade_log.json (software stop lives in
    `stop` / `trail_stop`). None if unreadable — the caller then says effectiveness is UNKNOWN."""
    try:
        d = json.loads(TRADE_LOG.read_text())
        return {str(t["symbol"]): t for t in (d.get("open") or [])
                if isinstance(t, dict) and t.get("symbol")}
    except Exception as e:
        logger.warning("trade_log.json unreadable (%s) — software-stop effectiveness UNKNOWN", e)
        return None


def scan_self_reports(since: dict, log_path: "Path | None" = None) -> dict:
    """{symbol: [matching log lines]} — an UNCAPPED pass over mtf_bot.log (the tail reader keeps only
    2,000 lines, but a 4h RTH window is ~5,500 lines on production). `since` = {symbol: aware UTC
    datetime}: only lines stamped at/after it (the holding's entry) count. Log stamps are UTC."""
    out: dict = {s: [] for s in since}
    if not since:
        return out
    tags = {f"[{s}]": s for s in since}
    floor = min(since.values()).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(log_path or BOT_LOG, errors="replace") as fh:
            for line in fh:
                if line[:19] < floor or "[" not in line:
                    continue
                for tag, sym in tags.items():
                    if (tag in line and line[:19] >= since[sym].strftime("%Y-%m-%d %H:%M:%S")
                            and _LEVEL_RE.search(line) and _STOPWORD_RE.search(line)
                            and _FAILWORD_RE.search(line) and not _NOT_EXIT_RE.search(line)):
                        out[sym].append(line.rstrip())
    except OSError as e:
        logger.warning("bot log unreadable for the self-report scan (%s)", e)
    return out


def _entry_utc(trade: "dict | None", fallback: datetime) -> datetime:
    """Aware UTC entry time of a tracker record, else `fallback` (the session open)."""
    try:
        t = datetime.fromisoformat(str((trade or {}).get("entry_time")))
        return (t if t.tzinfo else t.replace(tzinfo=PT)).astimezone(UTC)
    except (TypeError, ValueError):
        return fallback


def held_symbols(gt: dict, positions: "list | None") -> set:
    """Symbols to check as held right now: the live positions read (when it succeeded) UNION the
    ground truth's own `uncovered_at_close` symbols. The ground truth's window ends at its own (later)
    snapshot, so `uncovered_at_close` means held at that moment with no full resting broker stop. The
    union matters: positions are read BEFORE the ground truth (which may retry for ~1 min), so a fill in
    between is only in the ground truth — the live read may only ADD symbols, never filter one out
    (cold-2nd r2: read failed ⇒ checks silenced; r3: read stale ⇒ a new fill silenced)."""
    from_gt = {s for s, i in (gt.get("positions") or {}).items() if i.get("uncovered_at_close")}
    return from_gt | {p.get("symbol") for p in positions or []}


def software_stop_findings(gt: dict, positions: "list | None", open_trades: "dict | None",
                           self_reports: dict) -> tuple[list[dict], list[str]]:
    """Mid-session, SOFTWARE-ONLY-BY-DESIGN means LIVE exposure protected only by the bot's software
    stop — the loop running does not prove that stop is honoured (board masked-loss seat M1). For each
    such position still held: (a) a bot self-report of a stop/exit failure on the symbol since its entry
    (see scan_self_reports) → CRITICAL; (b) the live mark already beyond the tracker's stop → CRITICAL
    if the tracker itself recorded the breach (stop_breached), else HIGH (it may be between cycles).
    Returns (findings, symbols whose effectiveness could not be checked)."""
    findings: list[dict] = []
    unknown: list[str] = []
    if gt.get("status") != "OK":
        return findings, unknown
    marks = {}
    for p in positions or []:
        try:
            marks[p.get("symbol")] = float(p.get("current_price"))
        except (TypeError, ValueError):
            continue
    held = held_symbols(gt, positions)
    for sym, info in (gt.get("positions") or {}).items():
        if info.get("class") != "SOFTWARE-ONLY-BY-DESIGN" or sym not in held:
            continue
        hits = self_reports.get(sym) or []
        uncovered_now = bool(info.get("uncovered_at_close"))
        if hits:
            # CRITICAL only when the ground truth shows NO resting broker stop at the check moment; a
            # failure the bot later repaired (a broker stop IS resting now) stays visible as HIGH
            # (adversarial B1: SNOW 2026-08-10 — repaired overnight failure raised a false CRITICAL).
            findings.append({"severity": "critical" if uncovered_now else "high",
                             "title": (f"{sym}: bot reported a stop/exit failure — held with no broker stop"
                                       if uncovered_now else
                                       f"{sym}: bot reported a stop/exit failure today — broker stop resting at the check"),
                             "detail": hits[-1][:200]})
            if uncovered_now:
                continue
            # HIGH (a broker stop rests): still run the mark check — it may be CRITICAL on its own
            # (tracker-recorded breach); a self-report must never LOWER the alarm (cold-2nd r6).
        t = (open_trades or {}).get(sym)
        if not t:
            unknown.append(sym)
            continue
        try:
            stops = [float(x) for x in (t.get("stop"), t.get("trail_stop")) if x is not None]
        except (TypeError, ValueError):
            stops = []
        if not stops or sym not in marks:
            unknown.append(sym)
            continue
        long_ = str(t.get("direction", "long")).lower() == "long"
        eff = max(stops) if long_ else min(stops)
        mark = marks[sym]
        through = mark < eff if long_ else mark > eff
        if through:
            sev = "critical" if t.get("stop_breached") else "high"
            findings.append({"severity": sev,
                             "title": f"{sym}: price beyond the software stop, still held",
                             "detail": (f"{'long' if long_ else 'short'}: mark {mark:.2f} vs software stop "
                                        f"{eff:.2f}; "
                                        + ("no broker stop at the check" if uncovered_now
                                           else "a broker stop is resting at the check")
                                        + ("; tracker recorded the breach" if t.get("stop_breached") else ""))})
    return findings, unknown


def uncovered_now_findings(gt: dict, positions: "list | None") -> list[dict]:
    """Mid-session a position still UNCOVERED at the check moment but within the 2-min tolerance is
    classed COVERED by the ground truth (its held-into-close rule only fires at the session close).
    Board masked-loss seat R1: never let that clear silently — HIGH "re-check", CRITICAL if a stop order
    for it was REJECTED and it is not a core-only holding (nothing protects it in software)."""
    if gt.get("status") != "OK":
        return []
    held = held_symbols(gt, positions)
    out = []
    for sym, p in (gt.get("positions") or {}).items():
        cls = p.get("class")
        # BY-DESIGN whose owners include a non-core tier: an earlier core round trip (software stop by
        # design) can class the symbol BY-DESIGN while a later day-tier holding sits uncovered within the
        # 2-min tolerance (cold-2nd r4) — nothing protects that holding in software.
        mixed_design = cls == "SOFTWARE-ONLY-BY-DESIGN" and p.get("owners") != ["core"]
        if (cls != "COVERED" and not mixed_design) or not p.get("uncovered_at_close") or sym not in held:
            continue
        rejected = int(p.get("rejected_stop_orders") or 0)
        crit = rejected > 0 and p.get("owners") != ["core"]
        out.append({"severity": "critical" if crit else "high",
                    "title": f"{sym} uncovered right now (within fill latency) — re-check",
                    "detail": (f"{sym}: no resting broker stop at the check moment"
                               + (f"; {rejected} stop order(s) REJECTED" if rejected else "")
                               + f"; owners {'/'.join(p.get('owners') or [])}")})
    return out


def escalate_card_verdict(verdict: str, gt_alarms: list[dict]) -> str:
    """Broker ground-truth alarms can only RAISE the card verdict: critical → FAIL; high lifts
    PASS/UNKNOWN to WARN; low (or none) changes nothing."""
    if any(f.get("severity") == "critical" for f in gt_alarms):
        return "FAIL"
    if any(f.get("severity") == "high" for f in gt_alarms) and verdict in ("PASS", "UNKNOWN"):
        return "WARN"
    return verdict


def summarise_fills(fills: "list | None") -> dict:
    """Ground-truth trade activity from Alpaca fills (the entry-EVENT log is
    degraded — D1). Returns total fill count, distinct symbols, per-symbol
    buy/sell counts. None → available=False (caller notes it, never fabricates)."""
    if fills is None:
        return {"available": False, "n_fills": 0, "symbols": [], "by_symbol": {}}
    by_symbol: dict[str, dict] = {}
    for a in fills:
        sym = a.get("symbol", "?")
        side = a.get("side", "?")
        d = by_symbol.setdefault(sym, {"buy": 0, "sell": 0})
        if side in ("buy", "sell"):
            d[side] += 1
    return {"available": True, "n_fills": len(fills),
            "symbols": sorted(by_symbol), "by_symbol": by_symbol}


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info(f"Midday audit starting — {AUDIT_DATE} PT")

    events        = read_today_trade_events()
    log_lines     = read_bot_log_tail(hours=4)
    entry_anal    = analyse_entries(events)
    mri_anal      = analyse_mri(events)
    log_anal      = analyse_log(log_lines)
    pnl_anal      = analyse_pnl(events)
    postmortem    = run_signal_postmortem(events)

    # ── Live state (fetched once, reused for prompt + card) ──────────────────
    positions     = _fetch_live_positions()
    today_fills   = _fetch_today_fills()
    fills_sum     = summarise_fills(today_fills)

    # Stop coverage = BROKER GROUND TRUTH (Alpaca order + fill history, computed by code). It only
    # ADDS findings and can only RAISE the card verdict — it never removes/lowers an LLM finding.
    gt = collect_ground_truth()
    gt_block = ""
    gt_alarms: list[dict] = []
    gt_lows = ""
    gt_checked = ""
    try:
        from reporting import broker_ground_truth as _bgt
        gt_block = _bgt.render(gt)
        gt_alarms, gt_lows = _bgt.card_alarms(gt)
        if gt.get("status") == "OK":
            gt_checked = _bgt.checked_summary(gt)
    except Exception as _gt_e:
        logger.warning("broker ground truth render failed (%s) — stop coverage UNVERIFIED", _gt_e)
        gt = {"status": "UNKNOWN", "reason": str(_gt_e)[:200], "positions": {}}
        gt_alarms = [{"severity": "high", "title": "Broker stop coverage UNKNOWN — ground truth failed",
                      "detail": str(_gt_e)[:200]}]
    if gt.get("status") == "UNKNOWN":
        # DEGRADED fallback: name every held symbol with no resting stop right now (legs counted).
        # HIGH, never critical (cannot tell core-by-design from day-tier), never cleared.
        _unc = snapshot_uncovered(positions, _fetch_open_orders_nested())
        if _unc is None:
            gt_alarms.append({"severity": "high",
                              "title": "Degraded stop snapshot ALSO failed — positions/open orders unreadable",
                              "detail": "no stop coverage could be verified this run; check Alpaca directly"})
        for u in _unc or []:
            gt_alarms.append({"severity": "high",
                              "title": f"{u['symbol']} uncovered right now — owner/design unverified",
                              "detail": (f"{u['symbol']}: {u['side']} {u['qty']:g}sh, resting stop covers "
                                         f"{u['stop_qty']:g}sh (broker history unreadable)")})
    open_trades = _read_open_trades()
    _held = held_symbols(gt, positions)
    if positions is None and gt.get("status") == "OK":
        gt_alarms.append({"severity": "high",
                          "title": "Live positions unreadable — held set taken from broker ground truth",
                          "detail": ("software-stop mark checks NOT run; self-report and uncovered-now "
                                     "checks use the ground truth's own positions snapshot")})
    _session_open_utc = datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0).astimezone(UTC)
    # Floor at TODAY's session open: a carried position's entry may be days old, and a stop failure
    # from a prior evening that was since repaired is not today's exposure (adversarial B1).
    _since = {sym: max(_entry_utc((open_trades or {}).get(sym), _session_open_utc), _session_open_utc)
              for sym, p in (gt.get("positions") or {}).items()
              if p.get("class") == "SOFTWARE-ONLY-BY-DESIGN" and sym in _held}
    self_reports = scan_self_reports(_since)
    sw_alarms, sw_unknown = software_stop_findings(gt, positions, open_trades, self_reports)
    _rank = {"critical": 0, "high": 1}
    gt_alarms = sorted(sw_alarms + uncovered_now_findings(gt, positions) + gt_alarms,
                       key=lambda f: _rank.get(str(f.get("severity")), 2))
    if gt_block and _held:
        gt_block += ("\nHeld right now (live positions + ground-truth uncovered-now): " if positions is not None
                     else "\nHeld right now with no broker stop (ground truth; live read failed): ") + ", ".join(sorted(str(h) for h in _held))

    logger.info(
        f"Parsed {len(events)} events | "
        f"{entry_anal['entry_count']} entries | "
        f"{entry_anal['stop_hit_count']} stops | "
        f"{len(entry_anal['flagged_entries'])} flagged | "
        f"broker stop check={gt.get('status')} alarms={len(gt_alarms)} | "
        f"alpaca fills={fills_sum['n_fills']}"
    )

    # ── Write JSON report to logs/ ────────────────────────────────────────────
    report = {
        "audit_date":     AUDIT_DATE,
        "run_ts":         _now.isoformat(),
        "entry_analysis": entry_anal,
        "mri_analysis":   mri_anal,
        "log_analysis":   log_anal,
        "postmortem":     postmortem,
        "pnl_analysis":   pnl_anal,
        "stop_coverage":  dict(gt, schema="broker_ground_truth_v1",
                               alarms=gt_alarms, software_stop_unverified=sw_unknown),
        "alpaca_fills":   fills_sum,
    }
    _tmp = REPORT_PATH.with_suffix(".json.tmp")
    with open(_tmp, "w") as f:
        json.dump(report, f, indent=2, default=str)
    _tmp.replace(REPORT_PATH)
    logger.info(f"Report written: {REPORT_PATH}")

    # ── Determine alert severity ──────────────────────────────────────────────
    has_watchdog = log_anal["watchdog_count"] > 0
    has_flagged  = bool(entry_anal["flagged_entries"])
    high_stops   = entry_anal["stop_hit_rate"] >= STOP_HIT_WARN and entry_anal["entry_count"] > 0
    has_errors   = log_anal["error_count"] > 0
    session_loss = pnl_anal["total_pnl"] < -50   # flag sessions with significant realized loss
    has_naked    = any(f.get("severity") == "critical" for f in gt_alarms)   # ground-truth critical
    stop_concern = any(f.get("severity") == "high" for f in gt_alarms)       # ground-truth high → review

    if has_naked or has_watchdog or (has_flagged and high_stops) or session_loss:
        emoji = ":rotating_light:"
        severity = "ACTION REQUIRED"
    elif has_flagged or has_errors or stop_concern:
        emoji = ":warning:"
        severity = "REVIEW"
    else:
        emoji = ":white_check_mark:"
        severity = "CLEAN"

    # ── Gemini adversarial review (report file always written) ───────────────
    gemini_report = ""
    gemini_verdict = "UNKNOWN"
    gt_violations: list = []
    if GEMINI_API_KEY:
        logger.info("Running Gemini decision quality review...")
        gemini_prompt  = _build_gemini_prompt(entry_anal, mri_anal, log_anal,
                                              pnl_anal, log_lines, gt_block, fills_sum)
        gemini_report  = _call_gemini(gemini_prompt)
        gemini_verdict = _extract_verdict(gemini_report)
        # Detect-only: did the LLM still call a COVERED / BY-DESIGN symbol naked? (log + footer only)
        try:
            from reporting.broker_ground_truth import naked_claims_on_cleared
            # a quoted self-report for a BY-DESIGN symbol is PERMITTED by the midday prompt — not a violation
            gt_violations = [v for v in naked_claims_on_cleared(gemini_report, gt)
                             if not self_reports.get(v)]
        except Exception as _cmp_e:
            logger.warning("GT compliance check failed: %s", _cmp_e)
        if gt_violations:
            logger.warning("GT_COMPLIANCE (midday): LLM naked claim on COVERED/BY-DESIGN %s", gt_violations)

        GEMINI_REPORT.write_text(
            f"Midday Gemini Audit — {AUDIT_DATE}\n"
            f"Model: {GEMINI_MODEL} | Verdict: {gemini_verdict}\n"
            f"{'='*80}\n\n" + gemini_report,
            encoding="utf-8",
        )
        logger.info(f"Gemini report → {GEMINI_REPORT} | Verdict: {gemini_verdict}")
    else:
        logger.warning("GEMINI_API_KEY not set — skipping Gemini review.")

    # ── Slack — single Block Kit card (Rafael format-lock 2026-07-02) ─────────
    # One card replaces the previous two wall-of-text posts (stats + raw Gemini
    # dump). Unrealized MTM P&L from live positions; findings parsed from the
    # Gemini report; session stats in one context line. Legacy two-post path is
    # kept strictly as fallback so the audit never goes silent.
    sent = False
    try:
        from scripts.audit_slack import (build_pnl_fields, render_card,
                                         validate_no_pnl_rewrite, post_to_slack,
                                         findings_from_report)
        if positions is None:  # reuse the fetch from above; None → cannot build MTM card
            raise RuntimeError("live positions unavailable — cannot build MTM card")
        eod_dict = {}
        _eod_path = LOGS_DIR / f"eod_{AUDIT_DATE}.json"
        if _eod_path.exists():
            try:
                eod_dict = json.loads(_eod_path.read_text())
            except (json.JSONDecodeError, OSError):
                eod_dict = {}
        pnl = build_pnl_fields("midday", eod_dict, positions=positions)

        findings = findings_from_report(gemini_report) if gemini_report else []
        for wl in log_anal["watchdog_lines"]:
            findings.insert(0, {"severity": "high", "title": "Watchdog restart",
                                "detail": wl[:200]})
        # Broker ground-truth findings (deterministic, code-computed) go FIRST; they only ADD to the
        # LLM's findings and can only RAISE the verdict (critical → FAIL; high lifts PASS/UNKNOWN).
        findings = list(gt_alarms) + findings
        card_verdict = gemini_verdict if gemini_verdict != "UNKNOWN" else {
            "CLEAN": "PASS", "REVIEW": "WARN", "ACTION REQUIRED": "FAIL"}[severity]
        card_verdict = escalate_card_verdict(card_verdict, gt_alarms)
        mri_close = mri_anal.get("last_level", "?")
        context_line = (
            f"{entry_anal['entry_count']} entries · {entry_anal['exit_count']} exits · "
            f"{entry_anal['partial_count']} partials · stop-rate "
            f"{entry_anal['stop_hit_rate']:.0%} · MRI close {mri_close} · "
            f"{log_anal['error_count']} err / {log_anal['warning_count']} warn (4h)")
        dist = [f"✅ analysis — logs/{REPORT_PATH.name}"]
        if gemini_report:
            dist.append(f"✅ Gemini review — logs/{GEMINI_REPORT.name}")
        _gtp = gt.get("positions") or {}
        dist.append(f"🛡️ broker stop check: {gt.get('status')} · {len(_gtp)} positions · "
                    f"{len(gt_alarms)} alarm(s) · checked through "
                    f"{str(gt.get('window_pt', '')).split('-')[-1] or '?'} PT")
        if gt_checked:
            dist.append(f"🛡️ checked: {gt_checked}")
        if gt_lows:
            dist.append(f"🛡️ minor stop lapses (resolved): {gt_lows[:300]}")
        if sw_unknown:
            dist.append(f"⚠️ software-stop effectiveness UNKNOWN (no tracker stop): {', '.join(sw_unknown)}")
        if gt_violations:
            dist.append(f"⚠️ audit AI called {', '.join(gt_violations)} naked against broker data")
        payload = render_card("midday", AUDIT_DATE, card_verdict, pnl, findings,
                              dist_footer=dist, context_line=context_line)
        ok, reason = validate_no_pnl_rewrite(payload, pnl["injected_numbers"])
        if not ok:
            raise RuntimeError(f"P&L-rewrite validator blocked card: {reason}")
        sent = post_to_slack(payload) in (200, 204)
        if sent:
            logger.info("Slack Block Kit card posted.")
    except Exception as _card_err:
        logger.warning(f"Card render/post failed ({_card_err}) — legacy text fallback.")

    if not sent:
        _audit_label = "POST-MARKET AUDIT" if _now.hour >= 13 else "MIDDAY AUDIT"
        title = f"{_audit_label} [{severity}] — {AUDIT_DATE} PT"
        body  = build_slack_body(entry_anal, mri_anal, log_anal, pnl_anal,
                                 postmortem=postmortem)
        if gt_alarms:
            body = ("*BROKER STOP CHECK*\n" + "\n".join(
                f"  {f['severity'].upper()}: {f['title']} — {f['detail']}" for f in gt_alarms)
                + "\n\n" + body)
        _slack(title, body, emoji=emoji)
        if gemini_report:
            gemini_emoji = {
                "PASS": ":white_check_mark:", "WARN": ":warning:",
                "FAIL": ":rotating_light:", "UNKNOWN": ":grey_question:",
            }[gemini_verdict]
            gemini_slack_body = (
                f"{gemini_emoji} *Verdict: {gemini_verdict}*\n"
                + "\n".join(
                    line for line in gemini_report.splitlines()
                    if line.strip() and not line.startswith("```")
                )[:2000]
                + f"\n_Full report: {GEMINI_REPORT.name}_"
            )
            _slack(
                f"Midday Gemini Audit [{gemini_verdict}] — {AUDIT_DATE} PT",
                gemini_slack_body,
                emoji=gemini_emoji,
            )

    logger.info(f"Midday audit complete — severity={severity}")


if __name__ == "__main__":
    main()
