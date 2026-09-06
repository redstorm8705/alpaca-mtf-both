#!/usr/bin/env python3
"""
scripts/qhm_report.py — weekly QHM (quarterly-hold) STATUS report.

Replaces the per-account Claude routine that produced logs/quarterly_holds_research_*.md and silently
stopped after 2026-07-07. This runs as an OCI cron (weekly), so it always fires regardless of which
Claude account is active, and delivers to Slack (the cross-account channel Rafael reads).

Design record: logs/design_records/qhm_weekly_report_2026-08-22.md (BGG-hardened, Gro+GAI).

SAFETY: READ-ONLY on all trading state. It calls ONLY get_*/fetch_* (fetch_positions, fetch_account,
_fetch_alpaca_equity, get_cached_earnings_dates) — never submit/cancel/close. Separate process; it can
NEVER affect the bot. Slack delivery is decoupled from any file write (Slack-first).
"""
# ruff: noqa: E501
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PT = ZoneInfo("America/Los_Angeles")
logger = logging.getLogger("qhm_report")

# Populate ALPACA_* (and friends) from the repo .env, exactly as every other cron script does —
# reporting.pnl_ledger.fetch_positions reads os.getenv at call time, so without this a cron/ssh
# invocation (no ambient env) 401s and every hold falsely renders as CLOSED.
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass  # python-dotenv absent (non-OCI env) → rely on ambient environment
except Exception as _dotenv_err:
    # A present-but-unreadable/malformed .env (e.g. mode-600 owned by another user, or a mid-rotation
    # chmod) makes load_dotenv's open() raise a NON-ImportError. Left uncaught this runs at import,
    # ABOVE main()'s try/except, so it would kill the report with NO failure Slack and NO heartbeat —
    # the exact SILENT death the staleness guardrail exists to prevent. Degrade instead: continue with
    # the ambient env; any resulting missing creds surface loudly via the LIVE-P&L-UNAVAILABLE flag.
    logger.warning("qhm_report: load_dotenv skipped (%s) — using ambient environment", _dotenv_err)

_STATE_FILE = _ROOT / "data" / "state" / "quarterly_holds.json"
_HEARTBEAT = _ROOT / "logs" / "qhm_report_heartbeat.json"

# Read-only imports (get_*/fetch_* only — no order/execution surface).
# NOTE: data.fmp_client is imported LAZILY inside _earnings_status so a missing FMP dependency
# (e.g. `requests`) degrades earnings to UNKNOWN rather than crashing the whole report (BGG guardrail 1).
from reporting.pnl_ledger import fetch_positions, fetch_account          # noqa: E402
from reporting.metrics import _fetch_alpaca_equity                        # noqa: E402
from alerts import send_slack, send_slack_blocks                          # noqa: E402

_CONFIG_FILE = _ROOT / "data" / "state" / "quarterly_holds_config.json"


def _load_state() -> dict:
    """QHM intent/metadata store (FLAT {symbol: {...}}). {} on any failure (never raises)."""
    try:
        if not _STATE_FILE.exists():
            return {}
        d = json.loads(_STATE_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except Exception as e:
        logger.warning("qhm_report: state load failed (%s) — treating as empty", e)
        return {}


def _configured_universe() -> tuple[set[str], bool]:
    """Configured QHM pick universe (quarterly_holds_config.json 'picks'). This is the STABLE
    intended universe — needed for ghost detection (a configured pick held in Alpaca but absent from
    the live state file = UNMANAGED). NOT get_quarterly_hold_symbols(), which derives its set from
    the state file's ACTIVE entries and so can never surface a ghost.

    Returns (picks, config_ok). config_ok is False when the config file is missing/unreadable/has no
    picks — the caller MUST flag this, because an empty universe silently disables ghost/unmanaged
    detection (the safety case this report exists to surface). This is the ONE external read that
    guards that class, so its degradation must be flagged like every other read (cold-2nd finding)."""
    try:
        if not _CONFIG_FILE.exists():
            logger.warning("qhm_report: config file missing (%s) — ghost detection disabled", _CONFIG_FILE)
            return set(), False
        cfg = json.loads(_CONFIG_FILE.read_text())
        picks = {str(s).upper() for s in (cfg.get("picks") or {}).keys()}
        if not picks:
            logger.warning("qhm_report: config has no 'picks' — ghost detection disabled")
        return picks, bool(picks)
    except Exception as e:
        logger.warning("qhm_report: config universe load failed (%s) — ghost detection disabled", e)
        return set(), False


_CFG_EARN_CACHE: dict[str, str] = {}
_CFG_EARN_DONE = False


def _config_earnings_map() -> dict[str, str]:
    """{SYMBOL: earnings-date ISO} from quarterly_holds_config.json 'picks' (`earnings_exit_before`,
    the QHM's per-pick earnings deadline). The config carries a date for EVERY pick, so this covers
    holds (GE/GEV/LLY/…) that are NOT in config.WATCHLIST and thus absent from the FMP 10-day preload
    cache `get_cached_earnings_dates` reads — the root cause of the all-UNKNOWN earnings column
    (Rafael 2026-09-05). Memoized per run; {} on any failure; never raises; no API call."""
    global _CFG_EARN_DONE
    if not _CFG_EARN_DONE:
        _CFG_EARN_DONE = True
        try:
            cfg = json.loads(_CONFIG_FILE.read_text())
            for sym, row in (cfg.get("picks") or {}).items():
                if isinstance(row, dict) and row.get("earnings_exit_before"):
                    _CFG_EARN_CACHE[str(sym).upper()] = str(row["earnings_exit_before"])[:10]
        except Exception as e:
            logger.debug("qhm_report: config-earnings map load failed (%s)", e)
    return _CFG_EARN_CACHE


def _fetch_positions_map() -> tuple[dict, bool]:
    """{symbol: alpaca_position_dict}, and a live_ok flag. Never raises."""
    try:
        rows = fetch_positions() or []
        return {str(p.get("symbol", "")).upper(): p for p in rows if p.get("symbol")}, True
    except Exception as e:
        logger.warning("qhm_report: fetch_positions failed (%s) — live P&L unavailable", e)
        return {}, False


def _resolve_equity(pos_map: dict) -> tuple[float | None, str]:
    """Equity fallback hierarchy (NAV, not buying power): Alpaca equity → sum(market_value)+cash →
    None. Returns (equity, source_label)."""
    try:
        eq = _fetch_alpaca_equity()
        if eq is not None and eq > 0:
            return float(eq), "alpaca_equity"
    except Exception as e:
        logger.warning("qhm_report: _fetch_alpaca_equity failed (%s)", e)
    # Fallback: sum position market_value + account cash
    try:
        acct = fetch_account() or {}
        cash = float(acct.get("cash") or 0.0)
        mv = sum(float(p.get("market_value") or 0.0) for p in pos_map.values())
        if (cash + mv) > 0:
            return round(cash + mv, 2), "cash+market_value"
    except Exception as e:
        logger.warning("qhm_report: equity fallback failed (%s)", e)
    return None, "unavailable"


def _earnings_status(symbol: str, state_row: dict) -> tuple[str | None, str]:
    """(earnings_date_iso|None, categorical status). Source order: live FMP cache (authoritative, but
    only covers config.WATCHLIST symbols) → QHM config `earnings_exit_before` (covers EVERY pick,
    including holds not in WATCHLIST) → state earnings_gate_date → UNKNOWN.
    SAFE(>14d) / APPROACHING(<=14d) / LOCKED(<=3d) / PAST / UNKNOWN. Never raises."""
    edate: date | None = None
    today = datetime.now(PT).date()
    try:
        from data.fmp_client import get_cached_earnings_dates  # lazy: FMP dep optional
        dates = [d for d in (get_cached_earnings_dates(symbol) or []) if isinstance(d, date)]
        # Pick the NEXT (nearest future) earnings — do not assume list ordering (cold-2nd nit).
        future = sorted(d for d in dates if d >= today)
        if future:
            edate = future[0]
        elif dates:
            edate = sorted(dates)[-1]  # all past → most recent → PAST
    except Exception as e:
        logger.debug("qhm_report: cached earnings unavailable for %s (%s)", symbol, e)
    if edate is None:
        # Config-earnings fallback — the fix for the all-UNKNOWN column (Rafael 2026-09-05). Holds
        # GE/GEV/LLY are not in config.WATCHLIST, so get_cached_earnings_dates (10-day preload cache)
        # never has them and the FMP block above always misses. `earnings_exit_before` is the QHM's
        # per-pick earnings date (verified against each thesis, e.g. NVDA "~Aug 19" ↔ 2026-08-19).
        cfg_raw = _config_earnings_map().get(symbol.upper())
        if cfg_raw:
            try:
                edate = date.fromisoformat(cfg_raw)
            except Exception:
                edate = None
    if edate is None:
        raw = state_row.get("earnings_gate_date")
        if raw:
            try:
                edate = date.fromisoformat(str(raw)[:10])
            except Exception:
                edate = None
    if edate is None:
        return None, "UNKNOWN"
    days = (edate - today).days
    if days < 0:
        status = "PAST"
    elif days <= 3:
        status = "LOCKED"
    elif days <= 14:
        status = "APPROACHING"
    else:
        status = "SAFE"
    return edate.isoformat(), f"{status} ({days}d)"


def _days_held(state_row: dict) -> str:
    for key in ("created_at", "entry_day"):
        raw = state_row.get(key)
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))  # naive → assume ET (project convention)
                d0 = dt.astimezone(PT).date()
                return str((datetime.now(PT).date() - d0).days)
            except Exception:
                continue
    return "—"


def _safe_float(v) -> float | None:
    """float(v) or None — NEVER raises. State-file and broker values may be non-numeric strings; a
    raw float() on one of them would abort the whole report (the never-crash-on-partial-data contract)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_money(v) -> str:
    v = _safe_float(v)
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}" if v is not None else "—"


# ── Block Kit builders (rules/slack_format.md SLK01–SLK15) ──────────────────────
def _pnl_glyph(v) -> str:
    """🟢 for a non-negative unrealized P&L, 🔴 for a loss (SLK08 pre-attentive triage, paired with
    the signed number — never color alone)."""
    f = _safe_float(v)
    return "🟢" if (f is not None and f >= 0) else "🔴"


def _earn_glyph(earn_status: str) -> str:
    s = str(earn_status or "").upper()
    if s.startswith("LOCKED"):
        return "🔒 "
    if s.startswith("APPROACHING"):
        return "⚠️ "
    return ""


def _sec(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _ctx(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _hdr(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": str(text)[:150], "emoji": True}}


_DIV = {"type": "divider"}


def build_report() -> tuple[str, list, str]:
    """Returns (markdown_archive, slack_blocks, slack_fallback_text). Pure formatting from read-only
    data; never raises on partial data — degrades with explicit flags. The .md is the git archive;
    the blocks are Block Kit per rules/slack_format.md; the fallback carries the answer (SLK02)."""
    now = datetime.now(PT)
    today_iso = now.strftime("%Y-%m-%d")
    state = _load_state()
    pos_map, live_ok = _fetch_positions_map()
    universe, config_ok = _configured_universe()
    equity, eq_source = _resolve_equity(pos_map)

    flags: list[str] = []
    if not live_ok:
        flags.append("⚠️ LIVE P&L UNAVAILABLE — figures from state file only")
    if not state:
        flags.append("⚠️ QHM STATE FILE MISSING/EMPTY — reporting configured picks only")
    if not config_ok:
        flags.append("⚠️ QHM CONFIG UNAVAILABLE — ghost/unmanaged detection disabled")
    if equity is None:
        flags.append("⚠️ EQUITY UNAVAILABLE — %-of-equity omitted")
    # Pre-market note: a Mon 06:00 PT run is before the 06:30 PT open.
    if now.weekday() >= 5:
        flags.append("weekend snapshot — prices are last close")
    elif now.hour < 6 or (now.hour == 6 and now.minute < 30):
        flags.append("pre-market run — current_price may be prior close / pre-market print")

    state_upper = {str(k).upper(): v for k, v in state.items()}  # normalize keys (mixed-case safe)
    state_syms = set(state_upper.keys())
    active_rows: list[str] = []
    closed_rows: list[str] = []
    ghost_rows: list[str] = []
    active_recs: list[dict] = []   # structured per-hold records → Block Kit
    closed_recs: list[dict] = []
    ghost_recs: list[dict] = []
    book_unrealized = 0.0

    def _row_cells(sym, shares, basis, price, upl, uplpc, stop, dist_stop, tgt_pct, pct_eq,
                   headroom, earn, days, thesis, note):
        return (f"| {sym} | {shares} | {basis} | {price} | {upl} | {uplpc} | {stop} | {dist_stop} "
                f"| {tgt_pct} | {pct_eq} | {headroom} | {earn} | {days} | {thesis} | {note} |")

    for sym in sorted(state_syms | {s for s in pos_map if s in universe}):
        srow = state_upper.get(sym) or {}
        pos = pos_map.get(sym)
        _, earn_status = _earnings_status(sym, srow)
        days = _days_held(srow)
        # Sanitize the free-form thesis: a literal "|" / newline / CR would corrupt the markdown table
        # AND the Slack split("|") cell parse (cold-2nd nit).
        thesis = (str(srow.get("thesis_check_result") or "—")
                  .replace("|", "/").replace("\n", " ").replace("\r", " ").strip() or "—")
        # Coerce target_equity_pct once, safely — a non-numeric value must not abort the whole report.
        _tgt_raw = srow.get("target_equity_pct")
        try:
            tgt_pct = float(_tgt_raw) if _tgt_raw is not None else None
        except (TypeError, ValueError):
            tgt_pct = None
        tgt_pct_str = f"{tgt_pct*100:.0f}%" if tgt_pct is not None else "—"

        # Classify by KEY PRESENCE in state (not truthiness of the value — a present-but-empty
        # {} state entry must NOT be misread as a ghost; cold-2nd nit).
        in_state = sym in state_syms

        # GHOST: a configured pick held in Alpaca but not tracked in state.
        if not in_state and pos is not None:
            mv = _safe_float(pos.get("market_value")) or 0.0
            ghost_rows.append(
                f"| {sym} | {pos.get('qty','—')} | mkt {_fmt_money(mv)} | "
                f"{_fmt_money(pos.get('unrealized_pl'))} | ⚠️ UNMANAGED — in Alpaca, not in QHM state |"
            )
            ghost_recs.append({"sym": sym, "qty": pos.get("qty", "—"),
                               "mv": _fmt_money(mv), "upl": _fmt_money(pos.get("unrealized_pl"))})
            continue

        # ZOMBIE: tracked in state but no live position (stopped/closed externally).
        if in_state and pos is None:
            closed_rows.append(
                f"| {sym} | state={srow.get('state','?')} | qty_filled={srow.get('qty_filled','?')} "
                f"| entry {_fmt_money(srow.get('avg_entry_price'))} | ⚠️ CLOSED/LIQUIDATED_EXTERNALLY "
                f"— in state, no live position | {earn_status} |"
            )
            closed_recs.append({"sym": sym, "state": srow.get("state", "?"),
                                "qty_filled": srow.get("qty_filled", "?"),
                                "entry": _fmt_money(srow.get("avg_entry_price")), "earn": earn_status})
            continue

        if pos is None:
            continue  # not in state and no live position — nothing to report

        # ACTIVE: Alpaca is the P&L golden source; state is intent/metadata.
        assert pos is not None  # narrowed: pos is None → handled by the continues above
        a_qty = pos.get("qty", "—")
        a_basis = pos.get("avg_entry_price")
        _price_f = _safe_float(pos.get("current_price"))
        upl = pos.get("unrealized_pl")
        _uplpc_f = _safe_float(pos.get("unrealized_plpc"))
        mv = _safe_float(pos.get("market_value")) or 0.0
        book_unrealized += _safe_float(upl) or 0.0
        uplpc_str = f"{_uplpc_f*100:+.2f}%" if _uplpc_f is not None else "—"
        price_str = f"${_price_f:,.2f}" if _price_f is not None else "—"
        _stop_f = _safe_float(srow.get("stop_price"))
        stop_str = f"${_stop_f:,.2f}" if _stop_f is not None else "—"
        # distance-to-stop % = (price - stop)/price (all values already safe-floated)
        dist_stop = "—"
        if _stop_f is not None and _price_f is not None and _price_f > 0:
            dist_stop = f"{(_price_f - _stop_f)/_price_f*100:+.1f}%"
        # %-of-equity + dip-add headroom vs target$
        pct_eq = "—"
        headroom = "—"
        if equity is not None and equity > 0:
            pct_eq = f"{mv/equity*100:.1f}%"
            if tgt_pct is not None:
                headroom = _fmt_money(tgt_pct * equity - mv)
        # DISCREPANCY flag: state vs broker qty / basis. Crash-proof by construction (all values
        # safe-floated; no bare except to swallow — RC-3) and divide-by-zero guarded on state basis.
        note = ""
        _qf = _safe_float(srow.get("qty_filled"))
        _aq = _safe_float(a_qty)
        _ab = _safe_float(a_basis)
        _sb = _safe_float(srow.get("avg_entry_price"))
        if _qf is not None and _aq is not None and _qf != _aq:
            note = "⚠️ [DISCREPANCY] qty"
        elif _ab is not None and _sb not in (None, 0) and abs(_ab - _sb) / abs(_sb) > 0.005:
            note = "⚠️ [DISCREPANCY] basis"
        active_rows.append(_row_cells(
            sym, a_qty, _fmt_money(a_basis), price_str,
            _fmt_money(upl), uplpc_str, stop_str, dist_stop,
            tgt_pct_str, pct_eq, headroom, earn_status, days, thesis, note or "ok",
        ))
        active_recs.append({
            "sym": sym, "glyph": _pnl_glyph(upl),
            "upl_money": _fmt_money(upl), "uplpc": uplpc_str,
            "stop": stop_str, "dist": dist_stop,
            "pct_eq": pct_eq, "tgt": tgt_pct_str, "earn": earn_status,
            "earn_glyph": _earn_glyph(earn_status),
            "qty": a_qty, "basis": _fmt_money(a_basis), "price": price_str,
            "days": days, "note": note,
            "sort_pnl": (_safe_float(upl) if _safe_float(upl) is not None else 0.0),
        })

    eq_str = _fmt_money(equity) if equity is not None else "—"
    hdr = (
        f"# QHM Weekly Status — {today_iso}\n"
        f"_Generated {now.strftime('%Y-%m-%d %I:%M %p PT')} · equity {eq_str} ({eq_source}) · "
        f"book unrealized {_fmt_money(book_unrealized)}_\n"
    )
    if flags:
        hdr += "\n" + "\n".join(f"> {f}" for f in flags) + "\n"
    table_hdr = ("\n## Active holds\n"
                 "| Sym | Shares | Cost basis | Price | Unrl P&L | Unrl % | Stop | Dist-to-stop "
                 "| Target% | %-equity | Dip-add room | Earnings | Days held | Thesis | Flag |\n"
                 "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    md = hdr + table_hdr + ("\n".join(active_rows) if active_rows else "| _none_ |\n")
    if closed_rows:
        md += ("\n\n## Closed / exited holds (in state, no live position)\n"
               "| Sym | State | Qty filled | Entry | Status | Earnings |\n"
               "|---|---|---|---|---|---|\n" + "\n".join(closed_rows))
    if ghost_rows:
        md += ("\n\n## Unmanaged QHM-universe positions (in Alpaca, not in state)\n"
               "| Sym | Qty | Market value | Unrl P&L | Flag |\n"
               "|---|---|---|---|---|\n" + "\n".join(ghost_rows))
    md += "\n\n_Source: Alpaca /v2/positions (P&L golden) + data/state/quarterly_holds.json (intent) + FMP cached earnings. Read-only. Design: logs/design_records/qhm_weekly_report_2026-08-22.md_\n"

    # ── Block Kit output per rules/slack_format.md (SLK01–SLK15) ───────────────
    n_active = len(active_recs)
    locked = sum(1 for r in active_recs if str(r["earn"]).upper().startswith("LOCKED"))
    # SLK02 — the fallback carries the answer (phone notification preview + screen reader).
    fallback = f"📊 QHM Weekly {today_iso} · {n_active} hold(s) · book {_fmt_money(book_unrealized)}"
    if locked:
        fallback += f" · 🔒{locked} earnings-locked"
    if ghost_recs:
        fallback += f" · ⚠️{len(ghost_recs)} unmanaged"
    if (not live_ok) or (not config_ok) or (equity is None):
        fallback += " · ⚠️ data degraded"

    blocks: list = [_hdr(f"QHM Weekly Status · {today_iso}")]
    blocks.append(_ctx(f"Equity {eq_str} · book unrealized *{_fmt_money(book_unrealized)}* · "
                       f"{n_active} active hold(s)"))
    if flags:
        blocks.append(_sec("\n".join(flags)))
    blocks.append(_DIV)
    if active_recs:
        # SLK13 — sort by attention: biggest losers first.
        for r in sorted(active_recs, key=lambda x: x["sort_pnl"]):
            line1 = f"{r['glyph']} *{r['sym']}*  *{r['upl_money']} ({r['uplpc']})*"
            line2 = (f"🛡 Stop {r['stop']} · {r['dist']} cushion" if r["stop"] != "—"
                     else "🛡 Stop —")
            line3 = f"⚖️ {r['pct_eq']} of equity (cap {r['tgt']}) · {r['earn_glyph']}Earnings {r['earn']}"
            if r["note"]:
                line3 += f" · {r['note']}"
            blocks.append(_sec(f"{line1}\n{line2}\n{line3}"))
            blocks.append(_ctx(f"{r['qty']} sh · entry {r['basis']} → now {r['price']} · {r['days']}d held"))
    else:
        blocks.append(_sec("_No active QHM holds._"))
    if closed_recs:
        blocks.append(_DIV)
        blocks.append(_sec("*Closed / exited* (in state, no live position)"))
        for r in closed_recs:
            blocks.append(_ctx(f"⚪ *{r['sym']}* · was {r['qty_filled']} sh @ {r['entry']} · "
                               f"state {r['state']} · earnings {r['earn']}"))
    if ghost_recs:
        blocks.append(_DIV)
        blocks.append(_sec("*⚠️ Unmanaged* — in Alpaca, not in QHM state"))
        for r in ghost_recs:
            blocks.append(_ctx(f"⚠️ *{r['sym']}* · {r['qty']} sh · mkt {r['mv']} · P&L {r['upl']}"))
    blocks.append(_DIV)
    blocks.append(_ctx(f"Source: Alpaca /v2/positions (P&L) + quarterly_holds.json · "
                       f"{now.strftime('%b %d · %I:%M %p PT')}"))
    return md, blocks, fallback


def _write_heartbeat(ok: bool, note: str) -> None:
    try:
        _HEARTBEAT.write_text(json.dumps({
            "last_run_pt": datetime.now(PT).isoformat(), "ok": ok, "note": note,
        }))
    except Exception as e:
        logger.warning("qhm_report: heartbeat write failed (%s)", e)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    try:
        md, blocks, fallback = build_report()
    except Exception as e:
        logger.error("qhm_report: build_report failed (%s)", e, exc_info=True)
        try:
            send_slack(f":warning: QHM weekly report FAILED to build: {e}")
        except Exception:
            pass
        _write_heartbeat(False, f"build failed: {e}")
        return 1

    # Slack-FIRST — delivery is decoupled from the file write (BGG guardrail 6).
    # Block Kit per rules/slack_format.md (fallback carries the answer for the notification).
    try:
        slack_ok = bool(send_slack_blocks(blocks, fallback))
    except Exception as e:
        # send_slack_blocks is contracted never to raise; keep the guard as defense-in-depth.
        slack_ok = False
        logger.warning("qhm_report: Slack send raised unexpectedly (%s)", e)
    if slack_ok:
        logger.info("qhm_report: Slack blocks sent")
    else:
        # Honest logging (Rafael 2026-09-06): the prior code set slack_ok=True whenever the call did
        # not raise, so it logged "sent" and reported slack=ok in the heartbeat even when the webhook
        # was unset or every POST failed. send_slack_blocks now returns real delivery status.
        logger.warning("qhm_report: Slack delivery FAILED or not configured (see alerts warnings above)")

    # Atomic write of the archived .md (RC-5).
    md_ok = False
    out = _ROOT / "logs" / f"qhm_report_{datetime.now(PT).strftime('%Y-%m-%d')}.md"
    try:
        tmp = out.with_suffix(".md.tmp")
        tmp.write_text(md, encoding="utf-8")
        tmp.replace(out)
        md_ok = True
        logger.info("qhm_report: wrote %s", out)
    except Exception as e:
        logger.warning("qhm_report: md write failed (%s) — Slack already delivered", e)

    # Heartbeat reflects what ACTUALLY happened (cold-2nd nit: don't report "ok" on a failed write).
    _write_heartbeat(slack_ok or md_ok,
                     f"slack={'ok' if slack_ok else 'FAIL'} md={'ok' if md_ok else 'FAIL'}")
    return 0 if (slack_ok or md_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
