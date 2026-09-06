#!/usr/bin/env python3
"""
scripts/gex_weekend_report.py — WEEKEND GEX levels report (Rafael request 2026-09-06).

Delivers Friday's dealer-gamma picture for the names Rafael trades (SPY/QQQ + the Mag-7 +
memory-cycle day-tier underlyings) so the week can be planned over the weekend: regime label,
pin/centroid, call & put walls, net GEX, confidence — per symbol, to Slack + an archived md.

WHY A SEPARATE PATH (not refresh_gex): the live GEX writer computes spot from the real-time
last-trade + an NBBO spot-consistency GUARD (board+Gro+GAI 2026-07-26). On a weekend, single-name
quotes are missing or wide, so that guard (correctly) refuses them and every single name fails to
UNKNOWN — a live-safety behaviour we must NOT weaken. For a REPORT the authoritative weekend spot is
simply FRIDAY'S CLOSE, which is clean. This module therefore reuses data.gex's vetted compute
internals (_fetch_contracts / _fetch_snapshots / _compute_gex) with a Friday bar-close spot, and
NEVER touches the live gex_snapshot.json cache, the live guard, or any execution path.

SAFETY: READ-ONLY. No orders, no execution imports, no writes to data/state or the GEX cache. It
reads the Alpaca option chain (T1) + a daily bar (T1) and posts to Slack. A per-symbol failure is
isolated (that name renders UNKNOWN); the report never crashes the process.
"""
# ruff: noqa: E501
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PT = ZoneInfo("America/Los_Angeles")
logger = logging.getLogger("gex_weekend_report")

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass  # python-dotenv absent (non-OCI env) → rely on ambient environment
except Exception as _dotenv_err:
    # A present-but-unreadable .env must not kill the report at import (above main()'s guard).
    logger.warning("gex_weekend_report: load_dotenv skipped (%s) — using ambient environment", _dotenv_err)

import requests                                         # noqa: E402  (T1 REST, same as data.gex)
import data.gex as gex                                  # noqa: E402  (read-only: compute internals only)
from alerts import send_slack, send_slack_blocks        # noqa: E402

_HEARTBEAT = _ROOT / "logs" / "gex_weekend_report_heartbeat.json"


def _friday_close(symbol: str) -> float | None:
    """Friday's last price = spot for the weekend report. Alpaca latest bar (T1 REST, IEX feed);
    over a weekend this returns Friday's final bar. None on any failure — NEVER raises."""
    try:
        resp = requests.get(
            f"{gex._BASE_DATA}/v2/stocks/{symbol}/bars/latest",
            headers=gex._headers(), params={"feed": "iex"}, timeout=6.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            bar = data.get("bar") if isinstance(data, dict) else None
            c = bar.get("c") if isinstance(bar, dict) else None
            if isinstance(c, (int, float)) and c > 0:
                return float(c)
            return None
        logger.warning("gex_weekend_report: bar HTTP %s for %s", resp.status_code, symbol)
    except Exception as e:
        logger.warning("gex_weekend_report: bar fetch failed for %s (%s)", symbol, e)
    return None


def _universe() -> list[str]:
    """SPY/QQQ + Mag-7 + memory-cycle day-tier underlyings (deduped, order preserved). Mirrors
    data.gex's report set without the tracker→underlying map (these are the underlyings)."""
    raw = list(gex._MARKET_SYMBOLS) + list(gex._DAYTRADE_UNDERLYINGS)
    return list(dict.fromkeys(raw))


def _compute_symbol(symbol: str, date_gte: str, date_lte: str) -> dict:
    """Compute Friday-close-spot GEX for one symbol. Returns a normalized row dict; on any miss/error
    returns {'symbol', 'label': 'UNKNOWN', 'reason': ...}. NEVER raises (per-symbol isolation)."""
    try:
        spot = _friday_close(symbol)
        if not spot:
            return {"symbol": symbol, "label": "UNKNOWN", "reason": "no_close"}
        oi_map = gex._fetch_contracts(symbol, date_gte, date_lte)
        if not oi_map:
            return {"symbol": symbol, "label": "UNKNOWN", "reason": "no_contracts", "spot": spot}
        snapshots = gex._fetch_snapshots(list(oi_map))
        if not snapshots:
            return {"symbol": symbol, "label": "UNKNOWN", "reason": "no_snapshots", "spot": spot}
        gx = gex._compute_gex(snapshots, oi_map, spot)
        pin = gx.get("pin", {}) if isinstance(gx.get("pin"), dict) else {}
        return {
            "symbol": symbol,
            "label": gx.get("label", "UNKNOWN"),
            "spot": round(spot, 2),
            "centroid": pin.get("centroid"),
            "call_wall": pin.get("call_wall"),
            "put_wall": pin.get("put_wall"),
            "confidence": pin.get("confidence"),
            "flip_strike": gx.get("flip_strike"),
            "raw_gex_m": gx.get("raw_gex_m"),
            "contract_count": gx.get("contract_count"),
        }
    except Exception as e:  # per-symbol isolation — one bad name never blanks the report
        logger.warning("gex_weekend_report: compute failed for %s (%s)", symbol, e)
        return {"symbol": symbol, "label": "UNKNOWN", "reason": f"error:{str(e)[:60]}"}


def _regime_glyph(label: str | None) -> str:
    s = str(label or "").upper()
    if s == "POSITIVE":
        return "🟢"
    if s == "NEGATIVE":
        return "🔴"
    if s == "NEAR-FLIP":
        return "🟡"
    return "⚪"


def _num(v, dp: int = 2) -> str:
    if isinstance(v, (int, float)):
        return f"{v:,.{dp}f}"
    return "—"


def _gex_m(v) -> str:
    if isinstance(v, (int, float)):
        return f"{'+' if v >= 0 else '-'}${abs(v):,.0f}M"
    return "—"


def _sec(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _ctx(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _hdr(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": str(text)[:150], "emoji": True}}


_DIV = {"type": "divider"}


def build_report() -> tuple[str, list, str]:
    """Returns (markdown_archive, slack_blocks, slack_fallback_text). Pure formatting over the
    read-only computed rows. Block Kit per rules/slack_format.md (SLK01/SLK02)."""
    date_gte, date_lte = gex._expiry_range()
    today_iso = datetime.now(PT).strftime("%Y-%m-%d")
    rows = [_compute_symbol(sym, date_gte, date_lte) for sym in _universe()]
    ok = [r for r in rows if r.get("label") not in (None, "UNKNOWN")]

    # ── Slack (Block Kit) ──
    blocks: list = [_hdr(f"GEX Weekend Report · {today_iso}")]
    blocks.append(_ctx(f"Friday close · week expiry {date_lte} · {len(ok)}/{len(rows)} names\n"
                       f"🟢 pin/mean-revert · 🔴 trend/squeeze-prone · 🟡 near-flip"))
    blocks.append(_DIV)
    for r in rows:
        g = _regime_glyph(r.get("label"))
        sym = r.get("symbol")
        if r.get("label") in (None, "UNKNOWN"):
            blocks.append(_sec(f"{g} *{sym}* UNKNOWN · _{r.get('reason', 'no data')}_"))
            continue
        line1 = f"{g} *{sym}* {r.get('label')} · ${_num(r.get('spot'))}"
        line2 = (f"pin {_num(r.get('centroid'))} · walls {_num(r.get('put_wall'))}–{_num(r.get('call_wall'))} · "
                 f"GEX {_gex_m(r.get('raw_gex_m'))} · conf {_num(r.get('confidence'))}")
        blocks.append(_sec(f"{line1}\n{line2}"))
    blocks.append(_DIV)
    blocks.append(_ctx("Source: Alpaca option chain (Fri OI) + Fri close as spot · read-only · "
                       "not the live RTH signal (live GEX uses a guarded real-time spot)"))

    # ── Fallback (SLK02 — carries the answer for the phone/notification) ──
    fb_bits = [f"{_regime_glyph(r.get('label'))}{r.get('symbol')}" for r in rows if r.get("label") not in (None, "UNKNOWN")]
    fallback = f"GEX Weekend Report {today_iso} (Fri close): " + " ".join(fb_bits) if fb_bits else f"GEX Weekend Report {today_iso}: no computable names"

    # ── Markdown archive (a table is fine in the md file; Slack uses Block Kit) ──
    md_lines = [
        f"# GEX Weekend Report — {today_iso}",
        f"_Friday close as spot · week expiry {date_lte} · {len(ok)}/{len(rows)} names computed · read-only_",
        "",
        "| Sym | Regime | Spot | Pin | Call wall | Put wall | Net GEX | Conf | Contracts |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r.get("label") in (None, "UNKNOWN"):
            md_lines.append(f"| {r.get('symbol')} | UNKNOWN ({r.get('reason','')}) | — | — | — | — | — | — | — |")
        else:
            md_lines.append(
                f"| {r.get('symbol')} | {r.get('label')} | {_num(r.get('spot'))} | {_num(r.get('centroid'))} | "
                f"{_num(r.get('call_wall'))} | {_num(r.get('put_wall'))} | {_gex_m(r.get('raw_gex_m'))} | "
                f"{_num(r.get('confidence'))} | {r.get('contract_count', '—')} |")
    md_lines += ["", "Read: 🟢 POSITIVE gamma → dealers dampen moves, price pins/mean-reverts toward the centroid "
                 "(walls act as magnets). 🔴 NEGATIVE → dealers amplify, trend/squeeze-prone. 🟡 NEAR-FLIP → transitional.",
                 "", "_Read-only report; not the live RTH GEX signal (which uses a guarded real-time spot)._"]
    md = "\n".join(md_lines) + "\n"
    return md, blocks, fallback


def _write_heartbeat(ok: bool, note: str) -> None:
    try:
        _HEARTBEAT.write_text(json.dumps({"last_run_pt": datetime.now(PT).isoformat(), "ok": ok, "note": note}))
    except Exception as e:
        logger.warning("gex_weekend_report: heartbeat write failed (%s)", e)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    try:
        md, blocks, fallback = build_report()
    except Exception as e:
        logger.error("gex_weekend_report: build failed (%s)", e, exc_info=True)
        try:
            send_slack(f":warning: GEX weekend report FAILED to build: {e}")
        except Exception:
            pass
        _write_heartbeat(False, f"build failed: {e}")
        return 1

    # Slack-first (delivery decoupled from the file write). send_slack_blocks returns real status.
    try:
        slack_ok = bool(send_slack_blocks(blocks, fallback))
    except Exception as e:
        slack_ok = False
        logger.warning("gex_weekend_report: Slack send raised unexpectedly (%s)", e)
    if slack_ok:
        logger.info("gex_weekend_report: Slack blocks sent")
    else:
        logger.warning("gex_weekend_report: Slack delivery FAILED or not configured (see alerts warnings above)")

    md_ok = False
    out = _ROOT / "logs" / f"gex_weekend_report_{datetime.now(PT).strftime('%Y-%m-%d')}.md"
    try:
        tmp = out.with_suffix(".md.tmp")
        tmp.write_text(md, encoding="utf-8")
        tmp.replace(out)
        md_ok = True
        logger.info("gex_weekend_report: wrote %s", out)
    except Exception as e:
        logger.warning("gex_weekend_report: md write failed (%s)", e)

    _write_heartbeat(slack_ok or md_ok, f"slack={'ok' if slack_ok else 'FAIL'} md={'ok' if md_ok else 'FAIL'}")
    return 0 if (slack_ok or md_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
