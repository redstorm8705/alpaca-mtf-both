# ruff: noqa: E501 — HTML template strings are intentionally long; line-wrapping breaks HTML
"""
generate_dashboard.py
Writes dashboard.html from live Alpaca data + trade_log.json + bot state files.
Called automatically by main.py at the end of each run_cycle() scan.
Can also be run manually: python3 generate_dashboard.py

Reads:
  - Alpaca /account + /positions + /orders (live)
  - trade_log.json (open/closed trades, stats)
  - logs/hybrid_state.json (SPY event type, scans_left)
  - logs/eod_YYYY-MM-DD.json (today's P&L baseline)

Writes:
  - dashboard.html (atomic write via tmp file)
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from reporting.metrics import compute_lifetime_stats

# Load .env explicitly so API keys are available whether this module is
# imported from within main.py's process or run standalone. Without this,
# _load_alpaca() falls back to os.getenv() which returns empty strings if
# the calling shell never exported ALPACA_API_KEY / ALPACA_SECRET_KEY.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).parent / ".env", override=False)
except Exception as _dotenv_e:
    # dotenv optional — API keys may already be in environment from main.py
    print(
        "[generate_dashboard] dotenv load failed"
        f" (keys may still be in env): {_dotenv_e}",
        file=sys.stderr,
    )

# NOTE: No RTH block here — this module is imported by main.py and runs during
# trading hours by design. RTH guardrail #5 applies to standalone analysis
# scripts only, not to dashboard generators called by the bot itself.

ET  = ZoneInfo("America/New_York")
PT  = ZoneInfo("America/Los_Angeles")
ROOT = Path(__file__).parent.resolve()
LOG_DIR = ROOT / "logs"

logger = logging.getLogger("generate_dashboard")
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")


# ── Data loaders ─────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default  # expected on first run or missing state file — silent
    except Exception as _load_e:
        logger.debug("_load_json(%s): load failed — %s", path.name, _load_e)
        return default


def _load_trade_log():
    return _load_json(ROOT / "trade_log.json", {"open": [], "closed":[], "stats": {}})


def _load_hybrid_state():
    # P0-REGIME: apply the same guards as main.py's _load_hybrid_state().
    # Without this, a stale hybrid_state.json from yesterday shows a prior-day
    # event on the dashboard while the bot (which cleared on startup) shows CLEAR
    # on the scan page — contradictory states for the operator.
    data = _load_json(LOG_DIR / "hybrid_state.json", {})
    if data:
        saved_at   = data.get("saved_at", "")
        today_et   = datetime.now(ET).strftime("%Y-%m-%d")
        scans_left = int(data.get("scans_left", 0))
        if not saved_at.startswith(today_et) or scans_left <= 0:
            return {}
    return data


def _load_today_eod():
    today = datetime.now(PT).date().isoformat()
    return _load_json(LOG_DIR / f"eod_{today}.json", {})


def _load_bot_status():
    return _load_json(LOG_DIR / "bot_status.json", {})


def _load_market_news():
    return _load_json(LOG_DIR / "market_news.json", {"items":[]})


def _load_gex_snapshot():
    return _load_json(LOG_DIR / "gex_snapshot.json", {})


def _load_alpaca():
    """
    Fetch live account, positions, and open orders via direct Alpaca REST.
    Uses urllib.request — no alpaca-py SDK dependency, no MACL risk.
    Works from any Python version/process that has the keys in os.environ.
    """
    # Belt-and-suspenders: reload .env inside the function so keys are
    # guaranteed available regardless of import order or calling process.
    # override=True is intentional — if the shell exported empty strings for
    # these vars (e.g. via an unset export), this forces the .env values in.
    try:
        from dotenv import load_dotenv as _ld
        _ld(Path(__file__).parent / ".env", override=True)
    except Exception as _dotenv2_e:
        logger.debug(
            "_load_alpaca: dotenv reload failed (keys may still be in env): %s",
            _dotenv2_e,
        )
    import urllib.request as _ur
    import ssl as _ssl
    _api_key    = os.getenv("ALPACA_API_KEY", "").strip()
    _secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not _api_key or not _secret_key:
        return {
            "error": (
                "API keys not in environment. "
                "Restart the bot — main.py loads them at startup."
            ),
            "equity": 0, "cash": 0, "buying_power": 0, "last_equity": 0,
            "status": "unknown", "shorting": False,
            "positions": [], "orders":[],
        }
    try:
        import certifi as _cert
        _ctx = _ssl.create_default_context(cafile=_cert.where())
    except ImportError:
        _ctx = _ssl.create_default_context()

    _BASE    = "https://paper-api.alpaca.markets"
    _HEADERS = {"APCA-API-KEY-ID": _api_key, "APCA-API-SECRET-KEY": _secret_key}

    def _get(path):
        req = _ur.Request(f"{_BASE}{path}", headers=_HEADERS)
        with _ur.urlopen(req, context=_ctx, timeout=20) as r:
            return json.load(r)

    try:
        acct  = _get("/v2/account")
        raw_p = _get("/v2/positions")
        raw_o = _get("/v2/orders?status=open&limit=50")
        return {
            "equity":       float(acct.get("equity", 0) or 0),
            "last_equity":  float(acct.get("last_equity", 0) or 0),
            "cash":         float(acct.get("cash", 0) or 0),
            "buying_power": float(acct.get("buying_power", 0) or 0),
            "pdt_count":    int(acct.get("daytrade_count", 0) or 0),
            "status":       acct.get("status", "unknown"),
            "shorting":     acct.get("shorting_enabled", False),
            "positions": [
                {
                    "symbol":                 p["symbol"],
                    "qty":                    float(p["qty"]),
                    "side":                   p["side"],
                    "market_value":           float(p.get("market_value", 0) or 0),
                    "unrealized_pl":          float(p.get("unrealized_pl", 0) or 0),
                    "unrealized_intraday_pl": float(
                        p.get("unrealized_intraday_pl", 0) or 0),
                    "unrealized_plpc":        float(p.get("unrealized_plpc", 0) or 0),
                    "current_price":          float(p.get("current_price", 0) or 0),
                    "avg_entry_price":        float(p.get("avg_entry_price", 0) or 0),
                }
                for p in raw_p
            ],
            "orders":[
                {
                    "symbol":       o.get("symbol", "?"),
                    "side":         o.get("side", "?"),
                    "qty":          o.get("qty", "?"),
                    "type":         o.get("type") or o.get("order_type", "unknown"),
                    "status":       o.get("status", "?"),
                    "submitted_at": o.get("submitted_at", ""),
                }
                for o in raw_o[:15]
            ],
            "error": None,
        }
    except Exception as e:
        logger.error(f"Alpaca API load failed — dashboard will show zeros: {e}")
        return {
            "error": str(e), "equity": 0, "last_equity": 0,
            "cash": 0, "buying_power": 0,
            "status": "unknown", "shorting": False,
            "positions": [], "orders": [],
        }


# ── HTML builder ─────────────────────────────────────────────────────────────

def _col(val, pos_col="#30d158", neg_col="#ff3b30", zero_col="#636680"):
    if val is None:
        return zero_col
    return pos_col if val > 0 else (neg_col if val < 0 else zero_col)


def _pnl_str(val):
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}${val:,.2f}"


def _market_status_info(now_et: datetime) -> dict:
    from datetime import timedelta
    mins = now_et.hour * 60 + now_et.minute
    is_open = (now_et.weekday() < 5) and (9 * 60 + 30 <= mins < 16 * 60)

    if not is_open:
        d = 1
        while True:
            cand = now_et + timedelta(days=d)
            if cand.weekday() < 5:
                next_et = cand.replace(hour=9, minute=30, second=0, microsecond=0)
                break
            d += 1
        next_pt   = next_et.astimezone(PT)
        hr        = str(int(next_pt.strftime("%I")))
        ampm      = next_pt.strftime("%p").lower()
        open_time = f"{hr}:{next_pt.strftime('%M')}{ampm} PT"

        delta     = next_et - now_et
        total_m   = int(delta.total_seconds() // 60)
        h, m      = divmod(total_m, 60)
        countdown = f"{h}h {m}m" if h else f"{m}m"
    else:
        open_time = ""
        countdown = ""
        close_et  = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        delta_c   = close_et - now_et
        total_m   = int(delta_c.total_seconds() // 60)
        h, m      = divmod(total_m, 60)
        countdown = f"closes in {h}h {m}m" if h else f"closes in {m}m"

    return {"is_open": is_open, "open_time": open_time, "countdown": countdown}


def _scan_countdown(bot_status: dict, is_market_open: bool) -> str:
    try:
        ts_str = bot_status.get("timestamp", "")
        if not ts_str:
            return ""
        last = datetime.fromisoformat(ts_str).astimezone(ET)
        now  = datetime.now(ET)
        elapsed_s = int((now - last).total_seconds())
        elapsed_m = elapsed_s // 60
        interval  = 5 if is_market_open else 10
        remaining = max(0, interval * 60 - elapsed_s)
        rem_m     = remaining // 60
        rem_s     = remaining % 60
        ago_str   = f"{elapsed_m}m ago" if elapsed_m > 0 else "just now"
        nxt_str   = f"{rem_m}m {rem_s:02d}s" if rem_m < interval else f"{rem_m}m"
        return f"last scan {ago_str} · next in {nxt_str}"
    except Exception as _scd_e:
        logger.debug("_scan_countdown: timestamp parse failed — %s", _scd_e)
        return ""


def _build_mri_components(components: dict, mri_col: str) -> str:
    if not components:
        return ""
    _labels = {
        "vix_level":      "VIX Level",
        "vix_term":       "VIX/VIX3M",
        "jpy_carry":      "JPY Carry",
        "tlt_flight":     "TLT Flight",
        "credit_spread":  "Credit Sprd",
        "oil_shock":      "Oil Shock",
        "gold_flight":    "Gold Flight",
        "global_etf":     "Global ETF",
    }
    rows = ""
    for key, val in components.items():
        pts = val.get("score", 0) if isinstance(val, dict) else 0
        if pts <= 0:
            continue
        label = _labels.get(key, key)
        raw   = val.get("value", "") if isinstance(val, dict) else ""
        raw_s = f" ({raw:+.2f}%)" if isinstance(raw, (int, float)) else ""
        rows += (
            f'<div style="display:flex;justify-content:space-between;padding:4px 16px;'
            f'font-family:var(--mono);font-size:10px;border-bottom:1px solid #1e2240">'
            f'<span style="color:var(--dim)">{label}{raw_s}</span>'
            f'<span style="color:{mri_col};font-weight:700">+{pts}pts</span>'
            f'</div>'
        )
    if not rows:
        return ""
    return (
        f'<div style="background:#13162a;border-top:1px solid #1e2240">'
        f'<div style="padding:6px 16px 4px;font-family:var(--mono);font-size:9px;'
        f'letter-spacing:2px;color:var(--dim);text-transform:uppercase">MRI Drivers</div>'
        f'{rows}'
        f'</div>'
    )


def _build_gex_section(gex_data: dict, open_pos: list) -> str:
    """Render GEX panel rows for SPY/QQQ market anchors + current open positions."""
    if not gex_data or not gex_data.get("symbols"):
        return (
            '<div style="border-top:1px solid #1e2240">'
            '<div class="ph"><span class="pt">GEX</span></div>'
            '<div style="padding:20px 16px;font-family:var(--mono);font-size:11px;'
            'color:#636680">GEX data unavailable — ensure live_data_writer.py is running</div>'
            '</div>'
        )
    _gex_colors = {"POSITIVE": "#30d158", "NEAR-FLIP": "#ffd60a", "NEGATIVE": "#ff3b30"}
    sym_data = gex_data.get("symbols", {})
    ts_str   = gex_data.get("ts", "")

    rows = ""
    shown = set()
    for sym in ["SPY", "QQQ"] + [p["symbol"] for p in open_pos]:
        if sym in shown:
            continue
        shown.add(sym)
        d = sym_data.get(sym, {})
        if not d or d.get("error"):
            continue
        label    = d.get("label", "N/A")
        flip     = d.get("flip_strike")
        col      = _gex_colors.get(label, "#4a6a80")
        flip_str = f'  <span style="font-size:9px;color:var(--dim)">flip ${flip:,.0f}</span>' if flip else ""
        rows += (
            f'<tr><td style="color:var(--dim)">{sym} GEX</td>'
            f'<td><span style="color:{col};font-weight:700">{label}</span>{flip_str}</td></tr>'
        )

    if not rows:
        return ""
    return (
        f'<div style="border-top:1px solid #1e2240">'
        f'<div class="ph"><span class="pt">GEX</span>'
        f'<span class="pm" style="font-size:9px">{ts_str}</span></div>'
        f'<table><tbody>{rows}</tbody></table>'
        f'</div>'
    )


def _build_html(alpaca, trade_log, hybrid, eod, bot_status=None, market_news=None, gex=None):
    if bot_status is None:
        bot_status = {}
    if market_news is None:
        market_news = {"items":[]}

    now_et   = datetime.now(ET)
    now_pt   = now_et.astimezone(PT)
    _hr_pt   = str(int(now_pt.strftime("%I")))
    ts       = now_pt.strftime("%a %b %-d · ") + _hr_pt + now_pt.strftime(":%M:%S %p PT")
    _mkt     = _market_status_info(now_et)
    _scan_cd = _scan_countdown(bot_status, _mkt["is_open"])

    equity   = alpaca.get("equity", 0)
    bp       = alpaca.get("buying_power", 0)

    open_pos = alpaca.get("positions",[])
    orders   = alpaca.get("orders",[])

    # All-time P&L — Alpaca authoritative via compute_lifetime_stats(equity=equity).
    # Pass equity already fetched above so we skip the second Alpaca API call.
    # Per P&L sourcing rule: Alpaca equity is the sole source of truth for all-time P&L.
    try:
        # equity-based: pass already-fetched Alpaca equity (avoids second API call).
        # Falls back to EOD sum when equity=0 (API error) via skip_fetch guard.
        _lt = compute_lifetime_stats(
            equity=equity if equity > 0 else None,
            skip_fetch=bool(alpaca.get("error")) or equity <= 0,
        )
    except Exception as _lt_e:
        logger.warning(
            "_build_html: compute_lifetime_stats failed"
            " — all-time P&L shows equity-based estimate: %s",
            _lt_e,
        )
        _lt = {}
    all_pnl    = _lt.get("total_pnl", round(float(equity) - 2500.0, 2))
    all_wr     = _lt.get("win_rate", 0.0)
    all_trades = _lt.get("total_trades", 0)

    # S47e: Write lifetime P&L cache so monthly_review.py reads current data.
    # Only write when total_pnl is not None — prevents poison writes when
    # compute_lifetime_stats raises and returns {}. Zero P&L (0.0) correctly
    # persists (0.0 is not None → True). bool({}) == False guards empty dict.
    _total_pnl_val = _lt.get("total_pnl") if _lt else None
    if _lt and _total_pnl_val is not None:
        try:
            _lt_cache_path = LOG_DIR / "lifetime_pnl_cache.json"
            _lt_tmp = _lt_cache_path.with_suffix(".tmp")
            _lt_tmp.write_text(
                json.dumps({
                    "total_pnl":    round(float(_total_pnl_val), 4),
                    "win_rate":     round(float(_lt.get("win_rate", 0.0)), 2),
                    "total_trades": int(_lt.get("total_trades", 0)),
                    "ts":           datetime.now(ET).isoformat(),
                    "type":         "realized_eod",  # DS S52: consumers distinguish from prior equity-based cache
                }, indent=2),
                encoding="utf-8",
            )
            _lt_tmp.replace(_lt_cache_path)
            logger.info(
                "_build_html: lifetime_pnl_cache written — total_pnl=%.4f",
                _total_pnl_val,
            )
        except Exception as _lt_cache_e:
            logger.warning(
                "_build_html: lifetime_pnl_cache write failed"
                " — monthly_review.py will use stale/compute fallback: %s",
                _lt_cache_e,
            )

    # Unrealized P&L: from Alpaca's live open position data.
    # Filtered to symbols the bot has open so re-entries aren't double-counted.
    _open_syms_in_log = {t.get("symbol") for t in trade_log.get("open", [])}
    _open_unrealized  = sum(
        p.get("unrealized_intraday_pl", 0) for p in open_pos
        if p.get("symbol") in _open_syms_in_log
    )
    unrealized_pnl     = _open_unrealized  # still used: realized_pnl calc + kill switch gauge

    # Today P&L — Alpaca's authoritative account-level number.
    # equity - last_equity captures ALL realized gains (manual closes on Alpaca,
    # GTC fills, bot closes, partial exits) plus current unrealized P&L.
    # Never goes stale when a position closes mid-session — the previous approach
    # (eod["pnl_today"] + unrealized) zeroed out realized gains on any close
    # because the EOD file is only refreshed at AH, not intraday.
    alpaca_daily_pnl = equity - alpaca.get("last_equity", equity)
    today_pnl        = alpaca_daily_pnl

    # Cache today_pnl for scan_results.html bot health bar.
    # risk.daily_pnl resets to 0 on each bot restart; this Alpaca-authoritative
    # value (equity − last_equity) is the single source of truth for both pages.
    try:
        _pnl_cache_path = LOG_DIR / "daily_pnl_cache.json"
        _pnl_tmp = _pnl_cache_path.with_suffix(".tmp")
        _pnl_tmp.write_text(json.dumps({"daily_pnl": round(float(today_pnl), 2),
                                         "ts": datetime.now(ET).isoformat()}))
        _pnl_tmp.replace(_pnl_cache_path)
    except Exception as _cache_e:
        logger.warning(
            "_build_html: daily_pnl_cache write failed"
            " — scan_results.html health bar shows stale P&L: %s",
            _cache_e,
        )

    # Realized = Alpaca daily total minus current floating unrealized.
    # Correctly accounts for manual closes and any close that bypassed record_exit().
    realized_pnl     = round(today_pnl - unrealized_pnl, 2)
    realized_pnl_col = _col(realized_pnl)

    # SPY hybrid engine state
    spy_event   = hybrid.get("event_type", "")
    spy_scans   = hybrid.get("scans_left", 0)
    spy_mag     = hybrid.get("magnitude", 0.0)
    spy_active  = hybrid.get("active", False)
    spy_colors  = {
        "EXTREME":                  "#ff3b5c",
        "BROAD_GEO_CONFLICT":       "#ff6b35",
        "BROAD_GEO_ENERGY":         "#ff6b35",
        "BROAD_MACRO_SYSTEMIC":     "#ff3b5c",
        "BROAD_MACRO_CREDIT":       "#ff6b35",
        "BROAD_MACRO_FX":           "#ffd600",
        "BROAD_MACRO_MONETARY":     "#ffd600",
        "BROAD_GLOBAL_ASIA":        "#ffd600",
        "BROAD_GLOBAL_EU":          "#ffd600",
        "BROAD_TECHNICAL":          "#ffd600",
        "SECTOR":                   "#00e5ff",
    }
    spy_col  = spy_colors.get(spy_event, "#3a5068")
    spy_label = spy_event if spy_event else "CLEAR"

    # News + MRI state from bot_status.json
    _ns               = bot_status.get("news_summary", {})
    macro_risk_active = _ns.get("macro_risk_active", _ns.get("war_state_active", False))
    macro_event       = _ns.get("active_event_type", "")
    news_mult         = _ns.get("news_size_mult", 1.0)

    # MRI state (T1-B/T1-C)
    _mri              = _ns.get("mri", {})
    mri_score         = _mri.get("score", 0)
    mri_level         = _mri.get("level", "NORMAL")
    mri_size_floor    = _mri.get("size_floor", 1.0)

    _mri_colors = {
        "NORMAL":   "#636680",
        "ELEVATED": "#ffd600",
        "STRESSED": "#ff6b35",
        "HIGH":     "#ff3b5c",
        "CRITICAL": "#ff0040",
    }
    mri_col = _mri_colors.get(mri_level, "#3a5068")

    # P0-REGIME fallback: if hybrid_state.json was stale/absent (bot restart gap),
    # use spy_event_type from bot_status.json written by the after-hours path.
    if not spy_event:
        _bs_spy = bot_status.get("spy_event_type", "")
        if _bs_spy:
            spy_event  = _bs_spy
            spy_label  = _bs_spy
            spy_col    = spy_colors.get(_bs_spy, "#ff6b35")
            spy_active = True

    if not spy_event and macro_risk_active and macro_event:
        spy_label  = macro_event
        spy_col    = "#ff6b35"
        spy_active = True

    if spy_active and spy_event:
        spy_sub = f"Scans left: {spy_scans} · {spy_mag:+.2f}%"
        if macro_risk_active and macro_event:
            spy_sub += f" · ⚠ {macro_event}"
    elif spy_active and macro_risk_active:
        spy_sub = f"Size mult: {news_mult:.2f}x · Macro gate active"
    else:
        spy_sub = "No active SPY event"

    if mri_score > 0:
        spy_sub += f" · MRI {mri_score}/100"

    # Kill switch gauge — read from config so paper profile (15%) matches BoD-3 runtime override
    try:
        import config as _cfg
        max_loss_pct = getattr(_cfg, "MAX_DAILY_LOSS_PCT", 0.05)
    except Exception as e:
        logger.warning(f"Config import failed — kill switch display using fallback 5%: {e}")
        max_loss_pct = 0.05
    ks_limit     = equity * max_loss_pct
    ks_used_pct  = min(abs(today_pnl) / max(ks_limit, 1) * 100, 100) if today_pnl < 0 else 0
    ks_col       = "#ff3b30" if ks_used_pct >= 80 else ("#ffd60a" if ks_used_pct >= 50 else "#30d158")

    # Build open positions rows
    pos_rows = ""
    if open_pos:
        for p in open_pos:
            sym   = p["symbol"]
            side  = p["side"].upper()
            qty   = int(p["qty"])
            price = p["current_price"]
            upnl  = p["unrealized_pl"]            # Total P&L since entry (not intraday-only)
            ucol  = _col(upnl)
            sign  = "+" if upnl >= 0 else ""

            entry_price = p["avg_entry_price"]
            tl_open = {t["symbol"]: t for t in trade_log.get("open",[])} if isinstance(trade_log.get("open"), list) else {}
            td = tl_open.get(sym, {})
            stop_str   = f"${td['stop']:.2f}" if td.get("stop") else "—"
            target_str = f"${td['target']:.2f}" if td.get("target") else "—"
            score_str  = f"{td['score']}/12" if td.get("score") else "—"
            overnight  = "🌙" if td.get("overnight") else ""
            pos_rows += f"""
            <tr>
              <td style="color:#e2e4ee;font-weight:700">{sym} {overnight}</td>
              <td style="color:{'#30d158' if side=='LONG' else '#ff3b30'}">{side}</td>
              <td>{qty}</td>
              <td>${price:.2f}</td>
              <td style="color:#b8bdd4">${entry_price:.2f}</td>
              <td style="color:{ucol};font-weight:700">{sign}${abs(upnl):.2f}</td>
              <td style="color:#ffd60a">{stop_str}</td>
              <td style="color:#30d158">{target_str}</td>
              <td style="color:#b8bdd4">{score_str}</td>
            </tr>"""
    else:
        pos_rows = '<tr><td colspan="9" style="color:#3a5068;text-align:center;padding:20px">No open positions</td></tr>'

    order_rows = ""
    if orders:
        for o in orders[:10]:
            side_col = "#30d158" if o["side"] in ("buy", "long") else "#ff3b30"
            order_rows += f"""
            <tr>
              <td style="color:#e2e4ee;font-weight:700">{o['symbol']}</td>
              <td style="color:{side_col}">{o['side'].upper()}</td>
              <td>{o['qty']}</td>
              <td style="color:#b8bdd4">{o['type']}</td>
              <td style="color:#b8bdd4">{o['status']}</td>
            </tr>"""
    else:
        order_rows = '<tr><td colspan="5" style="color:#3a5068;text-align:center;padding:20px">No open orders</td></tr>'

    _risk_labels = {"HIGH_RISK": "HIGH RISK", "CAUTION": "CAUTION", "MONITOR": "MONITOR"}
    news_rows = ""
    _bot_items = bot_status.get("news_items", []) or _ns.get("news_items", []) or[]
    for item in _bot_items[:5]:
        risk  = item.get("risk", "MONITOR")
        badge = _risk_labels.get(risk, risk)
        news_rows += f"""<div class="news-item">
          <div class="news-headline">{item.get('headline','')}</div>
          <div class="news-meta">
            <span class="risk-badge risk-{risk}">{badge}</span>
            <span>{item.get('source','')}</span>
            <span>{item.get('time','')}</span>
          </div>
        </div>"""

    _mkt_items = market_news.get("items", [])
    _shown = len(_bot_items[:5])
    for item in _mkt_items[:max(0, 8 - _shown)]:
        news_rows += f"""<div class="news-item">
          <div class="news-headline">{item.get('headline','')}</div>
          <div class="news-meta">
            <span class="risk-badge risk-mkt">MARKET</span>
            <span>{item.get('source','')}</span>
            <span>{item.get('time','')}</span>
          </div>
        </div>"""
    if not news_rows:
        news_rows = '<div style="padding:20px 16px;font-family:var(--mono);font-size:11px;color:var(--muted)">No news items</div>'

    news_ts = market_news.get("generated", "")
    if news_ts:
        try:
            _ndt = datetime.fromisoformat(news_ts.replace("Z", "+00:00")).astimezone(PT)
            news_ts = _ndt.strftime("Updated %-I:%M %p PT")
        except Exception as _nts_e:
            logger.debug(
                "_build_html: news timestamp parse failed"
                " — timestamp display blank: %s",
                _nts_e,
            )
            news_ts = ""

    err_banner = ""
    if alpaca.get("error"):
        err_banner = f'<div style="background:#3a0010;border:1px solid #ff3b5c;border-radius:4px;padding:12px 20px;margin:16px 24px;font-family:monospace;font-size:12px;color:#ff3b5c">⚠ Alpaca API error: {alpaca["error"]}</div>'

    _weekly_files    = sorted(LOG_DIR.glob("weekly_*.html"))
    _today_pt        = datetime.now(PT).date()
    _cur_monday      = _today_pt - timedelta(days=_today_pt.weekday())
    _prev_monday     = _cur_monday - timedelta(weeks=1)
    _prev_week_fname = f"weekly_{_prev_monday.isoformat()}.html"
    _last_week_fname = (
        _prev_week_fname if (LOG_DIR / _prev_week_fname).exists()
        else (_weekly_files[-2].name if len(_weekly_files) >= 2 else "weekly_report.html")
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>MTF Bot — Dashboard</title>
<style>
:root{{--bg:#0d0f1a;--surface:#13162a;--border:#252847;--accent:#00e5ff;--green:#30d158;--red:#ff3b30;--yellow:#ffd60a;--muted:#8a94ae;--text:#e8ecff;--dim:#c8cce4;--mono:'SF Mono','Share Tech Mono',monospace;--sans:-apple-system,'SF Pro Text','Segoe UI',sans-serif}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px;min-height:100vh}}
header{{display:flex;align-items:center;justify-content:space-between;padding:13px 24px;border-bottom:1px solid var(--border);background:var(--surface);position:sticky;top:0;z-index:100}}
.logo{{font-size:16px;font-weight:700;color:var(--text);letter-spacing:-.01em}}
.logo span{{color:var(--muted);font-size:10px;font-weight:500;display:block;letter-spacing:.08em;text-transform:uppercase;margin-top:2px}}
.hdr-right{{display:flex;align-items:center;gap:16px;font-size:12px;color:var(--dim)}}
.pill{{display:flex;align-items:center;gap:6px;padding:5px 12px;border-radius:6px;border:1px solid;font-size:11px;font-weight:600;letter-spacing:.03em}}
.pill.open{{border-color:var(--green);color:var(--green);background:rgba(48,209,88,.08)}}
.pill.closed{{border-color:var(--muted);color:var(--muted);background:rgba(99,102,128,.08)}}
.pulse{{width:6px;height:6px;border-radius:50%;background:currentColor;animation:pulse 1.5s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.2}}}}
.kpi-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;padding:16px 24px;background:var(--bg);border-bottom:1px solid var(--border)}}
.kpi{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px 16px}}
.kpi-lbl{{font-size:9px;font-weight:700;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin-bottom:6px}}
.kpi-val{{font-size:22px;font-weight:700;color:var(--text);line-height:1.1}}
.kpi-sub{{font-size:10px;color:var(--dim);margin-top:4px}}
.health-bar{{display:flex;align-items:center;gap:16px;padding:8px 24px;background:var(--bg);border-bottom:1px solid var(--border);font-size:11px}}
.hb-item{{display:flex;align-items:center;gap:6px;color:var(--dim)}}
.hb-val{{font-weight:600}}
.main{{display:grid;grid-template-columns:1fr 360px;gap:16px;padding:16px 24px;background:var(--bg)}}
.panel{{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden}}
.ph{{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;border-bottom:1px solid var(--border)}}
.pt{{font-size:10px;font-weight:700;letter-spacing:.1em;color:var(--dim);text-transform:uppercase}}
.pm{{font-size:10px;color:var(--muted)}}
table{{width:100%;border-collapse:collapse}}
th{{font-size:9px;font-weight:600;letter-spacing:.08em;color:var(--muted);text-transform:uppercase;padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)}}
td{{padding:10px 12px;border-bottom:1px solid var(--border);font-size:12px;color:var(--text)}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:rgba(255,255,255,.03)}}
.ks-bar-wrap{{padding:12px 24px;border-bottom:1px solid var(--border);background:var(--bg)}}
.ks-lbl{{font-size:10px;font-weight:600;color:var(--dim);margin-bottom:6px;display:flex;justify-content:space-between}}
.ks-track{{height:6px;background:var(--border);border-radius:3px;overflow:hidden}}
.ks-fill{{height:100%;border-radius:3px;transition:width .3s}}
.links{{padding:10px;display:flex;flex-direction:column;gap:6px}}
.link-btn{{display:block;padding:10px 14px;border:1px solid var(--border);background:transparent;color:var(--text);text-decoration:none;font-size:12px;font-weight:500;border-radius:6px;transition:all .15s}}
.link-btn:hover{{border-color:var(--accent);color:var(--accent);background:rgba(10,132,255,.06)}}
.spy-badge{{display:inline-block;padding:3px 10px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.04em;border:1px solid currentColor;background:rgba(255,255,255,.03)}}
.mri-badge{{font-size:9px;padding:2px 8px}}
footer{{padding:12px 24px;font-size:11px;color:var(--muted);border-top:1px solid var(--border);display:flex;justify-content:space-between;background:var(--surface)}}
.news-feed{{padding:4px 0}}
.news-item{{padding:10px 14px;border-bottom:1px solid var(--border);display:flex;flex-direction:column;gap:4px}}
.news-item:last-child{{border-bottom:none}}
.news-headline{{font-size:12px;color:var(--text);line-height:1.45}}
.news-meta{{display:flex;align-items:center;gap:8px;font-size:10px;color:var(--muted)}}
.risk-badge{{padding:2px 8px;border-radius:4px;font-size:9px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}}
.risk-HIGH_RISK{{background:rgba(255,59,48,.15);color:#ff3b30;border:1px solid rgba(255,59,48,.3)}}
.risk-CAUTION{{background:rgba(255,214,10,.1);color:#ffd60a;border:1px solid rgba(255,214,10,.25)}}
.risk-MONITOR{{background:rgba(99,102,128,.15);color:#8e92ad;border:1px solid rgba(99,102,128,.3)}}
.risk-mkt{{background:rgba(10,132,255,.08);color:#6aabf7;border:1px solid rgba(10,132,255,.2)}}
@media(max-width:768px){{
  .kpi-grid{{grid-template-columns:repeat(2,1fr)!important;padding:10px!important;gap:8px}}
  .kpi{{padding:10px 12px}}
  .kpi-val{{font-size:18px!important}}
  .kpi-lbl{{font-size:8px}}
  .kpi-sub{{font-size:9px}}
  .main{{grid-template-columns:1fr!important;padding:10px!important;gap:10px}}
  header{{flex-wrap:wrap;gap:6px;padding:8px 12px}}
  .logo span{{display:none}}
  .hdr-right{{flex-wrap:wrap;gap:6px;font-size:10px}}
  footer{{flex-direction:column;gap:4px;font-size:9px}}
  .panel table{{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch}}
  th{{padding:6px 8px!important;font-size:9px!important;white-space:nowrap}}
  td{{padding:7px 8px!important;font-size:11px!important;white-space:nowrap}}
  .mob-nav{{display:flex!important}}
}}
.mob-nav{{display:none;position:fixed;bottom:0;left:0;right:0;z-index:200;
  background:var(--surface);border-top:1px solid var(--border);padding:8px 0}}
</style>
</head>
<body>

<div id="dash-ts" data-ts="{int(datetime.now(PT).timestamp())}" style="display:none"></div>

<header>
  <div class="logo">MTF Bot <span>COMMAND CENTER</span></div>
  <div class="hdr-right">
    <div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px">
      <span>{ts}</span>
      <span style="font-size:9px;color:var(--dim)">{_scan_cd}</span>
    </div>
    <div class="pill {'open' if _mkt['is_open'] else 'closed'}">
      <span class="pulse"></span>
      {'MARKET OPEN · ' + _mkt['countdown'] if _mkt['is_open'] else 'closed · opens ' + _mkt['open_time'] + ' · in ' + _mkt['countdown']}
    </div>
    <div class="pill {'open' if alpaca.get('status') == 'ACTIVE' else 'closed'}" style="font-size:9px;padding:3px 8px">
      {alpaca.get('status','UNKNOWN')}
    </div>
  </div>
</header>

{err_banner}

<!-- KPI Strip -->
<div class="kpi-grid">
  <div class="kpi">
    <div class="kpi-lbl">Equity</div>
    <div class="kpi-val" style="color:var(--accent)">${equity:,.2f}</div>
    <div class="kpi-sub">Buying power: ${bp:,.2f}</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">All-Time P&L</div>
    <div class="kpi-val" style="color:{_col(all_pnl)}">{_pnl_str(all_pnl)}</div>
    <div class="kpi-sub">{all_trades} trades · {all_wr:.0f}% win rate</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Realized P&L (today)</div>
    <div class="kpi-val" style="color:{realized_pnl_col}">{_pnl_str(realized_pnl)}</div>
    <div class="kpi-sub">{len(eod.get('trades', []))} closed trade(s)</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Open Positions</div>
    <div class="kpi-val" style="color:var(--accent)">{len(open_pos)}</div>
    <div class="kpi-sub">{len(orders)} order(s) · BP: ${bp:,.0f}</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Win Rate (all-time)</div>
    <div class="kpi-val" style="color:{_col(all_wr - 50)}">{all_wr:.0f}%</div>
    <div class="kpi-sub">{all_trades} closed trades · KS limit: ${-ks_limit:,.2f}</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">SPY Regime / MRI</div>
    <div class="kpi-val" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <span class="spy-badge" style="color:{spy_col};border-color:{spy_col}">{spy_label}</span>
      <span class="spy-badge mri-badge" style="color:{mri_col};border-color:{mri_col}">{mri_level} {mri_score}</span>
    </div>
    <div class="kpi-sub">{spy_sub}</div>
  </div>
</div>

<!-- Kill switch bar -->
<div class="ks-bar-wrap">
  <div class="ks-lbl">
    <span>Daily Loss vs Kill Switch ({max_loss_pct:.0%} limit = ${-ks_limit:,.2f})</span>
    <span style="color:{ks_col}">{ks_used_pct:.1f}% consumed</span>
  </div>
  <div class="ks-track">
    <div class="ks-fill" style="width:{ks_used_pct:.1f}%;background:{ks_col}"></div>
  </div>
</div>

<div class="main">
  <!-- Left: positions + orders -->
  <div class="panel">
    <div class="ph">
      <span class="pt">Open Positions</span>
      <span class="pm">{len(open_pos)} active</span>
    </div>
    <table>
      <thead><tr>
        <th>Symbol</th><th>Side</th><th>Qty</th><th>Current Price</th><th>Entry Price</th>
        <th>Float P&L</th><th>Stop</th><th>Target</th><th>Score</th>
      </tr></thead>
      <tbody>{pos_rows}</tbody>
    </table>

    <div class="ph" style="margin-top:1px">
      <span class="pt">Open Orders</span>
      <span class="pm">{len(orders)} pending</span>
    </div>
    <table>
      <thead><tr>
        <th>Symbol</th><th>Side</th><th>Qty</th><th>Type</th><th>Status</th>
      </tr></thead>
      <tbody>{order_rows}</tbody>
    </table>
  </div>

  <!-- Right: links + account info -->
  <div class="panel">
    <div class="ph">
      <span class="pt">Navigation</span>
    </div>
    <div class="links">
      <a class="link-btn" href="scan_results.html">→ Scan Results (Live)</a>
      <a class="link-btn" href="weekly_review.html">→ Weekly Review</a>
      <a class="link-btn" href="logs/{_last_week_fname}">→ Last Week Report</a>
    </div>

    <div class="ph" style="margin-top:1px">
      <span class="pt">Breaking News</span>
      <span class="pm">{news_ts}</span>
    </div>
    <div class="news-feed">{news_rows}</div>

    <div class="ph" style="margin-top:1px">
      <span class="pt">Bot State</span>
    </div>
    <table>
      <tbody>
        <tr><td style="color:var(--dim)">SPY Event</td><td><span style="color:{spy_col};font-weight:700">{spy_label}</span></td></tr>
        <tr><td style="color:var(--dim)">Scans to Clear</td><td>{spy_scans}</td></tr>
        <tr><td style="color:var(--dim)">SPY Move</td><td style="color:{'#00ff88' if spy_mag >= 0 else '#ff3b5c'}">{spy_mag:+.2f}%</td></tr>
        <tr><td style="color:var(--dim)">Kill Switch</td><td style="color:{ks_col}">{ks_used_pct:.1f}% used</td></tr>
        <tr><td style="color:var(--dim)">Last Refresh</td><td style="font-size:10px">{now_et.astimezone(PT).strftime('%-I:%M:%S %p PT')}</td></tr>
        <tr style="border-top:1px solid #1a2530">
          <td style="color:var(--dim)">MRI Score</td>
          <td><span style="color:{mri_col};font-weight:700">{mri_score}/100</span></td>
        </tr>
        <tr>
          <td style="color:var(--dim)">MRI Level</td>
          <td><span class="spy-badge" style="color:{mri_col};border-color:{mri_col};font-size:9px">{mri_level}</span></td>
        </tr>
        <tr>
          <td style="color:var(--dim)">MRI Size Floor</td>
          <td style="color:var(--green)">{mri_size_floor:.2f}x</td>
        </tr>

      </tbody>
    </table>
    {_build_gex_section(gex or {}, open_pos)}
  </div>
</div>

<footer>
  <span>Auto-refreshes every 30 seconds</span>
  <span>MTF Confluence Bot · Paper Trading · $2,500 account</span>
</footer>

<script>
// P2-STALE-WARN: client-side staleness detection
(function() {{
    var tsEl = document.getElementById('dash-ts');
    if (!tsEl) return;
    var genTs = parseInt(tsEl.getAttribute('data-ts'), 10);
    function checkStale() {{
        var ageSeconds = Math.floor(Date.now() / 1000) - genTs;
        var banner = document.getElementById('stale-warn-banner');
        if (ageSeconds > 90) {{
            if (!banner) {{
                banner = document.createElement('div');
                banner.id = 'stale-warn-banner';
                banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9999;background:#ff453a;color:#fff;text-align:center;padding:8px 16px;font-size:13px;font-weight:600;letter-spacing:0.02em;';
                banner.innerHTML = '⚠ DASHBOARD DATA IS STALE (' + Math.floor(ageSeconds / 60) + 'm ' + (ageSeconds % 60) + 's) — Live writer may be down. Refresh or restart live_data_writer.py';
                document.body.prepend(banner);
            }}
        }} else {{
            if (banner) banner.remove();
        }}
    }}
    checkStale();
    setInterval(checkStale, 15000);
}})();
</script>

<nav class="mob-nav">
  <a href="dashboard.html" style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;color:#0a84ff;text-decoration:none;font-size:10px;font-weight:600">
    <span style="font-size:18px">📊</span>Dashboard
  </a>
  <a href="scan_results.html" style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;color:#b8bdd4;text-decoration:none;font-size:10px;font-weight:600">
    <span style="font-size:18px">🔍</span>Scanner
  </a>
  <a href="weekly_review.html" style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;color:#b8bdd4;text-decoration:none;font-size:10px;font-weight:600">
    <span style="font-size:18px">📅</span>Weekly
  </a>
  <a href="options.html" style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;color:#b8bdd4;text-decoration:none;font-size:10px;font-weight:600">
    <span style="font-size:18px">⚡</span>Options
  </a>
</nav>

</body>
</html>
"""
    return html


def generate():
    """Load all data sources and write dashboard.html atomically."""
    alpaca      = _load_alpaca()
    trade_log   = _load_trade_log()
    hybrid      = _load_hybrid_state()
    eod         = _load_today_eod()
    bot_status  = _load_bot_status()
    market_news = _load_market_news()
    gex         = _load_gex_snapshot()

    html = _build_html(alpaca, trade_log, hybrid, eod,
                       bot_status=bot_status, market_news=market_news, gex=gex)

    out_path = ROOT / "dashboard.html"
    tmp_path = out_path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(html)
        tmp_path.replace(out_path)
        logger.info(f"dashboard.html written ({len(html):,} bytes)")
    except Exception as e:
        logger.error(f"Failed to write dashboard.html: {e}")
        raise


if __name__ == "__main__":
    generate()
