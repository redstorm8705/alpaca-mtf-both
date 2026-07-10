# ruff: noqa: E501, E701
"""
scan_to_html.py
Scores the full 25-ticker watchlist, ranks by confluence score,
writes scan_results.html — open directly in Chrome, no server needed.

Usage:
    python3 scan_to_html.py           # scan once, open browser
    python3 scan_to_html.py --watch   # rescan 30min (open) / 4hr (closed)
"""
import os
import sys
import time
import argparse
from ui_tokens import LIVE_CLOCK_HTML  # live-clock rule (2026-07-06)
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

logger = logging.getLogger("scan_to_html")
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import config
    from data.fetcher import fetch_multi_timeframe, fetch_bars
    from strategy.confluence import prepare_df, score_long_signal, score_short_signal
    from indicators.momentum import get_momentum_summary
    from indicators.macd import macd_bullish_cross, macd_bearish_cross
    from indicators.moving_averages import ema_bullish_cross, ema_bearish_cross
except ImportError as _e:
    print(f"Import error: {_e}")
    print("Ensure you are running from the bot root directory with dependencies installed.")
    raise  # re-raise so ImportError propagates normally — sys.exit() here would crash the bot
def _market_open_by_time():
    """Infer market status from ET time — no API call needed."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    mins = now.hour * 60 + now.minute
    return (now.weekday() < 5) and (9*60+30 <= mins < 16*60)

try:
    from execution.broker import is_market_open
except Exception as _import_e:
    logger.warning(
        "[scan_to_html] is_market_open import failed"
        " — falling back to time-based check: %s",
        _import_e,
    )
    is_market_open = _market_open_by_time
from datetime import timedelta  # noqa: E402

ET       = ZoneInfo("America/New_York")
PT       = ZoneInfo("America/Los_Angeles")
OUT_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_results.html")

# ── Composite regime module-level cache (30-min TTL) ─────────────────────────
_composite_regime_cache: dict = {"data": None, "ts": None}


def _get_composite_regime_display() -> dict:
    """
    Returns composite regime dict for HTML header display.
    Lazy-imports RegimeDetector to avoid startup cost.
    Cached 30 minutes at module level. Returns None on failure.
    """
    now = datetime.now(ET)
    cached_ts = _composite_regime_cache["ts"]
    if (cached_ts is not None
            and (now - cached_ts).total_seconds() < 1800):
        return _composite_regime_cache["data"]
    try:
        from strategy.volatility_regime import RegimeDetector as _RD
        result = _RD().get_composite_regime()
        _composite_regime_cache["data"] = result
        _composite_regime_cache["ts"]   = now
        return result
    except Exception as _cr_err:
        logger.warning("[scan] composite_regime fetch failed — returning None: %s", _cr_err)
        return None  # type: ignore[return-value]


def _fetch_implied_range(symbol: str) -> "dict | None":
    """
    Fetch ATM straddle price from nearest weekly options expiry via yfinance.
    Returns {"price": float, "low": float, "high": float, "expiry": str}
    or None on failure (fail silently — bar is omitted).
    """
    try:
        from data.alpaca_data import get_latest_trade as _glt
        price = _glt(symbol)
        if price is None or price <= 0:
            return None
        price = float(price)
        import yfinance as yf  # type: ignore[import-untyped]
        ticker = yf.Ticker(symbol)

        expirations = ticker.options
        if not expirations:
            return None

        # Nearest expiry from today — weekday filter prevents phantom Sat/Sun entries
        # (yfinance occasionally lists weekend dates for SPY/ETF chains; fetching those
        # chains returns all-NaN bid/ask/IV because no trading occurs. P5-L2 fix.)
        today  = datetime.now(ET).date()
        future = [e for e in expirations
                  if datetime.strptime(e, "%Y-%m-%d").date() >= today
                  and datetime.strptime(e, "%Y-%m-%d").weekday() < 5]   # Mon–Fri only
        if not future:
            return None
        nearest = future[0]

        chain = ticker.option_chain(nearest)
        calls = chain.calls.copy()
        puts  = chain.puts.copy()

        # Guard: empty chain (zero-OI or freshly listed expiry) → .iloc[0] IndexError
        if calls.empty or puts.empty:
            return None

        calls["_diff"] = abs(calls["strike"] - price)
        puts["_diff"]  = abs(puts["strike"]  - price)
        ac = calls.nsmallest(1, "_diff").iloc[0]
        ap = puts.nsmallest(1,  "_diff").iloc[0]

        call_bid, call_ask = float(ac["bid"]), float(ac["ask"])
        put_bid,  put_ask  = float(ap["bid"]), float(ap["ask"])
        call_mid = (call_bid + call_ask) / 2 if call_bid > 0 else float(ac["lastPrice"])
        put_mid  = (put_bid  + put_ask)  / 2 if put_bid  > 0 else float(ap["lastPrice"])

        straddle = call_mid + put_mid
        if straddle <= 0:
            return None
        return {
            "price":  price,
            "low":    round(price - straddle, 2),
            "high":   round(price + straddle, 2),
            "expiry": nearest,
        }
    except Exception as _ir_e:
        logger.debug("_fetch_implied_range(%s): options fetch failed — %s", symbol, _ir_e)
        return None


# PDT rule removed — accounts <$25K exempt per SEC/FINRA rule amendment (board vote S50 28-0).
# _pdt_reset_display() deleted S52.

# Top 50 by market cap across S&P500 + Nasdaq 100 (Q1 2026)
# Breadth across 50 gives a meaningful regime signal
def fetch_vix() -> "float | None":
    """Fetch current VIX from Yahoo Finance via yfinance. Returns float or None.

    P5-M2 fix: replaced raw requests.get to query1.finance.yahoo.com with yfinance
    Ticker.fast_info — eliminates Guardrail 1 violation and Yahoo endpoint fragility.
    """
    try:
        import yfinance as _yf_vix
        _vix_fi = _yf_vix.Ticker("^VIX").fast_info
        _vix_px = float(_vix_fi.last_price or 0)
        return round(_vix_px, 2) if _vix_px > 0 else None
    except Exception as _vix_err:
        logger.debug("[scan] VIX fetch failed — returning None: %s", _vix_err)
        return None


def vix_regime(vix: "float | None"):
    """Returns (label, color, sub) for VIX level."""
    if vix is None:
        return "VIX —", "#4a5070", "unavailable"
    if vix < 15:
        return f"VIX {vix:.1f}", "#30d158", "LOW — risk-on, full size"
    if vix < 20:
        return f"VIX {vix:.1f}", "#30d158", "NORMAL — standard sizing"
    if vix < 25:
        return f"VIX {vix:.1f}", "#ffd60a", "ELEVATED — reduce size 25%"
    if vix < 30:
        return f"VIX {vix:.1f}", "#ff9f0a", "HIGH — reduce size 50%"
    return f"VIX {vix:.1f}", "#ff3b30", "EXTREME — no new positions"


def to_py(obj):
    if isinstance(obj, dict):  return {k: to_py(v) for k,v in obj.items()}
    if isinstance(obj, list):  return [to_py(i) for i in obj]
    if isinstance(obj, bool):  return bool(obj)
    t = type(obj).__name__
    if "bool"  in t: return bool(obj)
    if "int"   in t: return int(obj)
    if "float" in t: return float(obj)
    return obj


def calc_atr(daily_df, period=14):
    try:
        df = daily_df.copy()
        df["pc"] = df["close"].shift(1)
        df["tr"] = df.apply(
            lambda r: max(r["high"]-r["low"],
                          abs(r["high"]-r["pc"]),
                          abs(r["low"]-r["pc"])), axis=1)
        return float(df["tr"].rolling(period).mean().iloc[-1])
    except Exception as _atr_e:
        logger.debug("calc_atr: computation failed — %s", _atr_e)
        return None


def avg_volume(daily_df, period=20):
    try:
        return float(daily_df["volume"].tail(period).mean())
    except Exception as _vol_e:
        logger.debug("avg_volume: computation failed — %s", _vol_e)
        return None


def _scan_ticker_with_timeout(symbol, timeout=20):
    """Wrapper that enforces a timeout on scan_ticker using threading.

    Replaces the previous signal.SIGALRM implementation (U-1).
    SIGALRM only works on the main thread — when scan workers run in a
    ThreadPoolExecutor (or any non-main thread), signal.alarm() raises:
        'signal only works in main thread'
    This caused silent write_scan_html failures across every cycle when
    the bot ran from a thread. threading.Thread + join(timeout) works in
    any thread context.

    The stalled thread is left as daemon=True — it will be reaped when the
    process exits and cannot block shutdown.
    """
    import threading

    _result_holder = [None]
    _error_holder  = [None]

    def _target():
        try:
            _result_holder[0] = scan_ticker(symbol)
        except Exception as _e:
            _error_holder[0] = _e

    _t = threading.Thread(target=_target, daemon=True)
    _t.start()
    _t.join(timeout=timeout)

    _timeout_result = dict(
        symbol=symbol, long_score=0, short_score=0,
        long_signal=False, short_signal=False,
        long_conditions={}, short_conditions={},
        daily_bias="unknown", momentum=None,
        price=None, pct_change=None, atr=None,
        volume=None, avg_vol=None, vol_ratio=None,
        stop_long=None, target_long=None,
        stop_short=None, target_short=None,
        stop_pct_long=None, target_pct_long=None,
        stop_pct_short=None, target_pct_short=None,
        rr=None, max_score=sum(config.SCORE_WEIGHTS.values()),
    )

    if _t.is_alive():
        # Thread still running — timed out
        _timeout_result["error"] = f"scan_ticker timed out after {timeout}s"
        return _timeout_result

    if _error_holder[0] is not None:
        _timeout_result["error"] = str(_error_holder[0])
        return _timeout_result

    return _result_holder[0]


def scan_ticker(symbol):
    MAX = sum(config.SCORE_WEIGHTS.values())
    r = dict(symbol=symbol, long_score=0, short_score=0,
             long_signal=False, short_signal=False,
             long_conditions={}, short_conditions={},
             daily_bias="unknown", momentum=None,
             price=None, pct_change=None, atr=None,
             volume=None, avg_vol=None, vol_ratio=None,
             stop_long=None, target_long=None,
             stop_short=None, target_short=None,
             stop_pct_long=None, target_pct_long=None,
             stop_pct_short=None, target_pct_short=None,
             rr=None, max_score=MAX, error=None)
    try:
        raw = fetch_multi_timeframe(symbol)
        if not raw:
            r["error"] = "no_data"
            return r
        tf  = {k: prepare_df(v) for k,v in raw.items()}
        lr  = score_long_signal(symbol,  tf, config.TradeMode.INTRADAY)
        sr  = score_short_signal(symbol, tf, config.TradeMode.INTRADAY)
        r.update(long_score=int(lr["score"]),   short_score=int(sr["score"]),
                 long_signal=bool(lr["signal"]), short_signal=bool(sr["signal"]),
                 long_conditions={k:bool(v) for k,v in lr["conditions"].items()},
                 short_conditions={k:bool(v) for k,v in sr["conditions"].items()},
                 daily_bias=str(lr.get("bias","unknown")))

        intra = tf.get(config.TF_15M)
        daily = tf.get(config.TF_DAILY)

        # Price — intraday preferred, daily close fallback when market closed
        if intra is not None and not intra.empty:
            r["price"]  = float(intra["close"].iloc[-1])
            r["volume"] = float(intra["volume"].sum()) if "volume" in intra.columns else None
        elif daily is not None and not daily.empty:
            r["price"] = float(daily["close"].iloc[-1])

        if daily is not None and len(daily) >= 2:
            prev = float(daily["close"].iloc[-2])
            curr = float(daily["close"].iloc[-1])
            r["pct_change"] = round((curr-prev)/prev*100, 2)

            # Volume vs 20-day average
            av = avg_volume(daily)
            if av:
                r["avg_vol"] = av
                today_vol = float(daily["volume"].iloc[-1])
                r["vol_ratio"] = round(today_vol / av, 2)

            # ATR stops + targets — use active config (main.py applies profile overrides at startup)
            atr = calc_atr(daily)
            if atr and r["price"]:
                sm = config.INTRADAY_STOP_ATR_MULT
                tm = config.INTRADAY_TARGET_ATR_MULT
                e  = r["price"]
                sl = round(e - atr*sm, 2)
                tp = round(e + atr*tm, 2)
                ss = round(e + atr*sm, 2)
                ts = round(e - atr*tm, 2)
                r.update(
                    atr=round(atr,2),
                    stop_long=sl,   target_long=tp,
                    stop_short=ss,  target_short=ts,
                    stop_pct_long=round((e-sl)/e*100, 2),
                    target_pct_long=round((tp-e)/e*100, 2),
                    stop_pct_short=round((ss-e)/e*100, 2),
                    target_pct_short=round((e-ts)/e*100, 2),
                    rr=round(tm/sm, 1)
                )
            r["momentum"] = to_py(get_momentum_summary(daily))

        # ── Weekly bias gate (display + entry filter) ─────────────────────
        try:
            from strategy.signal_generator import _get_weekly_bias
            wb = _get_weekly_bias(symbol)
        except Exception as _wb_e:
            logger.warning(
                "scan_ticker(%s): weekly_bias fetch failed"
                " — weekly filter gate will be skipped: %s",
                symbol,
                _wb_e,
            )
            wb = None
        r["weekly_bias"]          = wb
        r["weekly_bias_filtered"] = False
        if wb is not None:
            if wb == "BEARISH" and r["long_signal"]:
                r["long_signal"]          = False
                r["weekly_bias_filtered"] = True
            elif wb == "BULLISH" and r["short_signal"]:
                r["short_signal"]         = False
                r["weekly_bias_filtered"] = True

    except Exception as ex:
        r["error"] = str(ex)[:80]
    return r


def run_scan(tickers):
    print(f"\n[{datetime.now(PT).strftime('%-I:%M:%S %p PT')}] Scanning {len(tickers)} tickers...", flush=True)
    results = []
    for i, sym in enumerate(tickers):
        r    = _scan_ticker_with_timeout(sym, timeout=20)
        best = max(r["long_score"], r["short_score"])
        pct  = int((i+1)/len(tickers)*100)
        sig  = (" ↑" if r["long_signal"] else "")+(" ↓" if r["short_signal"] else "")
        px   = f"  ${r['price']:.2f}" if r["price"] else ""
        vr   = f"  vol {r['vol_ratio']}x" if r["vol_ratio"] else ""
        print(f"  [{pct:3d}%] {sym:<6} {best}/{r['max_score']}{sig}{px}{vr}", flush=True)
        results.append(r)
        # Delta-of-signal SHADOW (Cedar concept) — observational ONLY, gated by
        # config.DELTA_SCORING_ENABLED (False = log-only). record() never raises
        # and returns 0 in shadow mode, so it cannot affect scoring/sizing/entries.
        try:
            from strategy.delta_shadow import record as _delta_record
            _delta_record(sym, "long",  r.get("long_score", 0),  r.get("long_conditions", {}))
            _delta_record(sym, "short", r.get("short_score", 0), r.get("short_conditions", {}))
        except Exception:
            pass
    try:
        open_now = bool(is_market_open())
    except Exception as _mktopen_e:
        logger.warning("run_scan: is_market_open() failed — falling back to time-based check: %s", _mktopen_e)
        open_now = _market_open_by_time()
    longs  = [r["symbol"] for r in results if r["long_signal"]]
    shorts = [r["symbol"] for r in results if r["short_signal"]]
    print(f"  ↑ {longs or 'none'}   ↓ {shorts or 'none'}", flush=True)
    return dict(scan_time=datetime.now(ET).isoformat(), market_open=open_now, results=results)


# ── HTML constants ─────────────────────────────────────────────────────────────
COND_KEYS = ["daily_above_150sma","daily_above_200sma","ema13_above_ema30",
             "macd_bullish_cross","rsi_in_range","price_near_vwap","momentum_12_1"]
COND_DESC = {"daily_above_150sma":"Price above 150-day SMA",
             "daily_above_200sma":"Price above 200-day SMA",
             "ema13_above_ema30": "EMA 13 above EMA 30",
             "macd_bullish_cross":"MACD dual-TF agreement",
             "rsi_in_range":      "RSI in range (40-75)",
             "price_near_vwap":   "Price near / above VWAP",
             "momentum_12_1":     "12-1mo momentum (Jegadeesh-Titman)"}
BIAS_MAP  = {"strong_bull":("▲▲ STRONG BULL","#30d158"),
             "bull":       ("▲ BULL",        "#30d158"),
             "neutral":    ("◆ NEUTRAL",     "#ffd60a"),
             "bear":       ("▼ BEAR",        "#ff3b30"),
             "strong_bear":("▼▼ STRONG BEAR","#ff3b30"),
             "unknown":    ("—",             "#5a6080")}

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0f1a;color:#e8ecff;font-family:-apple-system,"SF Pro Text",sans-serif;font-size:13px;line-height:1.4}
table{width:100%;border-collapse:collapse}
thead th{padding:7px 14px;text-align:left;font-size:10px;font-weight:500;color:#8a94ae;
  letter-spacing:.08em;text-transform:uppercase;background:#0d0f1a;
  border-bottom:1px solid #252847;white-space:nowrap;position:sticky;top:64px;z-index:5}
.mrow{border-bottom:1px solid #1e2240;cursor:pointer;transition:background .1s}
.mrow:hover{background:#1e2240!important}
.det{display:none;background:#13162a;border-bottom:2px solid #252847}

.bar-bg{width:100%;height:4px;background:#252847;border-radius:2px;margin-top:4px}
.bar-fill{height:100%;border-radius:2px}
.label{font-size:10px;color:#8a94ae;letter-spacing:.06em;text-transform:uppercase;margin-bottom:6px}
.val{font-size:15px;font-weight:700;color:#e8ecff}
.sub{font-size:10px;color:#8a94ae;margin-top:2px}
.cond-row{display:flex;align-items:center;gap:7px;margin-bottom:5px}
.cond-icon{width:15px;height:15px;border-radius:2px;display:flex;align-items:center;
  justify-content:center;font-size:9px;font-weight:700;flex-shrink:0}
.grid4{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:20px;padding:18px 24px}
.divider{width:1px;background:#252847;margin:0 4px}
.scan-pill{display:flex;align-items:center;gap:8px;padding:5px 12px;border-radius:20px;
  border:1px solid;font-size:11px;font-weight:600;letter-spacing:.08em}
.scan-pill.open{border-color:#30d158;color:#30d158}
.scan-pill.closed{border-color:#8a94ae;color:#8a94ae}
.scan-pulse{width:7px;height:7px;border-radius:50%;background:currentColor;
  animation:scanpulse 1.5s infinite}
@keyframes scanpulse{0%,100%{opacity:1}50%{opacity:.2}}
@media(max-width:768px){
  .grid4{grid-template-columns:1fr 1fr!important;gap:10px;padding:12px 14px}
  thead th{font-size:9px;padding:5px 8px}
  td{padding:6px 8px;font-size:11px}
  .scan-pill{font-size:10px;padding:4px 8px}
}
"""

def sc(s, MAX):
    p = s/MAX
    if p>=.75: return "#30d158"
    return "#4a5070"

def cf(s, MAX):
    p = s/MAX*100
    if p>=75: return "STRONG",   "#30d158","rgba(48,209,88,.12)"
    if p>=58: return "HIGH",     "#0a84ff","rgba(10,132,255,.12)"
    if p>=42: return "MODERATE", "#ffd60a","rgba(255,214,10,.1)"
    return          "LOW",      "#4a5070","#0f1220"

def direction_tag(r):
    """Returns (label, color) — no backgrounds, no borders."""
    ls, ss = r["long_signal"], r["short_signal"]
    bias   = r.get("daily_bias", "unknown")
    if ls and ss:   return "CONFLICT", "#ff9f0a"
    if ls or bias in ("bull", "strong_bull"):  return "BULL",    "#30d158"
    if ss or bias in ("bear", "strong_bear"):  return "BEAR",    "#ff3b30"
    return "NEUTRAL", "#8a8fa8"

def decision_tag(r):
    """Returns (label, color) based on conviction tiers (9/12 minimum).
    Tags: SKIP · LONG/SHORT (FULL) · LONG/SHORT (½) · HOLD (Bucket A only)
          LONG/SHORT · WK BEAR/BULL SKIP (weekly-bias filtered)
    """
    if r.get("weekly_bias_filtered"):
        wb = r.get("weekly_bias", "")
        if wb == "BEARISH":
            return "LONG · WK BEAR SKIP",  "#ff9f0a"
        elif wb == "BULLISH":
            return "SHORT · WK BULL SKIP", "#ff9f0a"
        return f"SKIP · WK {wb}", "#ff9f0a"
    ls, ss = r["long_signal"], r["short_signal"]
    score  = max(r["long_score"], r["short_score"])
    sym    = r.get("symbol", "")
    is_bucket_a = sym in getattr(config, "BUCKET_A_TICKERS", set())
    if score < config.CONVICTION_SKIP_BELOW:              return "SKIP",         "#4a5070"
    if is_bucket_a:                                        return "HOLD",         "#0a84ff"
    if score >= config.CONVICTION_FULL_MIN:
        if ls and not ss:                                  return "LONG (FULL)",  "#30d158"
        if ss and not ls:                                  return "SHORT (FULL)", "#ff3b30"
    if score >= config.CONVICTION_HALF_MIN:
        if ls and not ss:                                  return "LONG (½)",     "#30d158"
        if ss and not ls:                                  return "SHORT (½)",    "#ff3b30"
    return "SKIP", "#4a5070"

def vol_html(vr):
    if vr is None: return '<span style="color:#b8bdd4">—</span>'
    color = "#ff9f0a" if vr >= 2.0 else "#8a8fa8"
    return f'<span style="color:{color};font-weight:600">{vr:.1f}x</span>'

def pct_span(p, suffix=""):
    if p is None: return '<span style="color:#b8bdd4">—</span>'
    s = "+" if p>=0 else ""
    c = "#30d158" if p>=0 else "#ff3b30"
    return f'<span style="color:{c};font-weight:600">{s}{p:.2f}{suffix}</span>'

def mom_val(m):
    if not m: return "—","#4a5070"
    v = m.get("momentum_12_1")
    if v is None: return "—","#4a5070"
    s = "+" if v>=0 else ""
    return f"{s}{v:.1f}%", "#30d158" if v>=0 else "#ff3b30"


def conviction_score(r):
    """Composite ranking: score% × volume × 52wk proximity × signal bonus."""
    MAX_  = r["max_score"]
    score = max(r["long_score"], r["short_score"])
    base  = score / MAX_
    vr    = r.get("vol_ratio") or 1.0
    vol_m = min(1.0 + (vr - 1.0) * 0.4, 1.4) if vr > 1.0 else max(0.7, vr)
    pct52 = (r.get("momentum") or {}).get("pct_from_52wk_high")
    if pct52 is not None:
        prox_m = 1.2 if pct52 >= -5 else (1.0 if pct52 >= -15 else 0.7)
    else:
        prox_m = 1.0
    sig_m = 1.3 if (r["long_signal"] or r["short_signal"]) else 1.0
    return round(base * vol_m * prox_m * sig_m, 4)


def build_rows(sorted_r, open_now=False, idx_offset=0, pm_extra=frozenset(), pm_all=frozenset(), confirm_gate=None, open_trades=None):
    out = ""
    for _i, r in enumerate(sorted_r):
        idx = idx_offset + _i
        # Show direction with fired signal; if neither/both, show whichever score is higher
        if r["long_signal"] and not r["short_signal"]:
            use_short = False
        elif r["short_signal"] and not r["long_signal"]:
            use_short = True
        else:
            use_short = r["short_score"] > r["long_score"]
        direction  = "short" if use_short else "long"
        score      = r["short_score"] if use_short else r["long_score"]
        conds      = r["short_conditions"] if use_short else r["long_conditions"]
        MAX        = r["max_score"]
        sym        = r["symbol"]

        # Signal dot color
        bl, bc = BIAS_MAP.get(r["daily_bias"], ("—", "#4a5070"))
        if r["daily_bias"] in ("strong_bull", "bull"):   dot_c = "#30d158"
        elif r["daily_bias"] in ("strong_bear", "bear"): dot_c = "#ff3b30"
        else:                                             dot_c = "#4a5070"

        # Decision label
        buy_tag, buy_c = decision_tag(r)

        # Momentum
        mv, mc = mom_val(r.get("momentum"))

        # Score color
        s_col = sc(score, MAX)

        # Entry/Stop/Target for dropdown
        sl  = r.get(f"stop_{direction}")
        tp  = r.get(f"target_{direction}")
        sp  = r.get(f"stop_pct_{direction}")
        tpp = r.get(f"target_pct_{direction}")
        rr  = r.get("rr")
        entry = r["price"]

        # Conditions for dropdown
        cond_rows = "".join([
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">'
            f'<span style="font-size:13px;color:{("#30d158" if conds.get(k) else "#4a5070")}">{"✓" if conds.get(k) else "✗"}</span>'
            f'<span style="font-size:12px;color:{("#c8d8e8" if conds.get(k) else "#4a5070")}">{COND_DESC[k]}</span>'
            f'</div>'
            for k in COND_KEYS
        ])

        # Trade math for dropdown
        if entry and sl and tp:
            trade_detail = (
                f'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:10px">'
                f'<div><div style="font-size:10px;color:#b8bdd4;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px">Entry</div>'
                f'<div style="font-size:14px;font-weight:700;color:#e2e4ee">${entry:,.2f}</div></div>'
                f'<div><div style="font-size:10px;color:#b8bdd4;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px">Stop ({sp:.1f}%)</div>'
                f'<div style="font-size:14px;font-weight:700;color:#ff3b30">${sl:,.2f}</div></div>'
                f'<div><div style="font-size:10px;color:#b8bdd4;text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px">Target (+{tpp:.1f}%)</div>'
                f'<div style="font-size:14px;font-weight:700;color:#30d158">${tp:,.2f}</div></div>'
                f'</div>'
                f'<div style="font-size:11px;color:#b8bdd4">R:R <span style="color:#0a84ff;font-weight:700">1:{rr}</span>'
                f' · ATR <span style="color:#e2e4ee">${r["atr"]:.2f}</span>'
                f' · Stop basis: {config.PROFILES["paper"]["INTRADAY_STOP_ATR_MULT"]}×ATR</span></div>'
            )
        else:
            trade_detail = '<span style="color:#b8bdd4;font-size:11px">Trade data unavailable</span>'

        # Regime context for dropdown
        mom     = r.get("momentum") or {}
        pct52   = mom.get("pct_from_52wk_high")
        rvol    = mom.get("realized_vol_20d")
        pct52_str = f'{pct52:+.1f}%' if pct52 is not None else "—"
        pct52_c   = "#30d158" if pct52 is not None and pct52 > -5 else ("#ffd60a" if pct52 is not None and pct52 > -15 else "#ff3b30")
        rvol_str  = f'{rvol*100:.0f}% ann.' if rvol is not None else "—"
        rvol_c    = "#ff3b30" if rvol is not None and rvol > 0.40 else ("#ffd60a" if rvol is not None and rvol > 0.25 else "#30d158")

        err_html = f'<div style="font-size:10px;color:#ff9f0a;margin-top:2px">⚠ {r["error"]}</div>' if r.get("error") else ""

        _gate_count = (confirm_gate or {}).get(sym, 0)

        # ── Pinned-row position badge — shown when SPY/QQQ are in open_trades ─
        _trade = (open_trades or {}).get(sym)
        if _trade and r.get("price"):
            _t_qty   = _trade.get("qty_remaining", _trade["qty"])
            _t_dir   = _trade["direction"]
            _t_entry = _trade["entry_price"]
            _t_pnl   = (r["price"] - _t_entry if _t_dir == "long"
                        else _t_entry - r["price"]) * _t_qty
            _t_pnl_c = "#30d158" if _t_pnl >= 0 else "#ff3b30"
            _t_sign  = "+" if _t_pnl >= 0 else ""
            _pos_badge = (
                f'<div style="font-size:9px;margin-top:3px">'
                f'<span style="color:#00e5ff;font-weight:700;letter-spacing:.05em">IN POSITION</span>'
                f'<span style="color:#636680"> · </span>'
                f'<span style="color:#b8bdd4">{_t_qty}sh @ ${_t_entry:,.2f}</span>'
                f'<span style="color:{_t_pnl_c};font-weight:700"> {_t_sign}${_t_pnl:,.2f}</span>'
                f'</div>'
            )
        else:
            _pos_badge = ""

        _is_pm = sym in pm_all
        _pm_badge = (
            '<span style="font-size:8px;font-weight:700;padding:1px 5px;border-radius:3px;'
            'background:rgba(255,214,10,.15);color:#ffd60a;letter-spacing:.06em;'
            'margin-left:4px">PM MOVER</span>'
        ) if _is_pm else ""
        _row_style = (
            'background:transparent;outline:1px dashed rgba(255,214,10,0.4);outline-offset:-1px'
            if _is_pm else 'background:transparent'
        )

        out += f"""
<tr id="row-{idx}" class="mrow" onclick="tog({idx})" style="{_row_style}">
  <td style="padding:10px 14px;width:160px">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <span style="font-size:16px;line-height:1;color:{dot_c}">●</span>
      <span style="font-weight:700;font-size:14px;color:#e2e4ee;letter-spacing:.02em">{sym}</span>
      {_pm_badge}
    </div>
    {_pos_badge}
    {err_html}
  </td>
  <td style="padding:10px 14px;width:110px">
    <span style="font-size:13px;font-weight:700;color:#e2e4ee">{("${:,.2f}".format(entry)) if entry else "—"}</span>
    <span style="font-size:10px;color:{"#30d158" if r.get("pct_change") and r["pct_change"]>=0 else "#ff3b30"};margin-left:4px">{("{:+.1f}%".format(r["pct_change"])) if r.get("pct_change") is not None else ""}</span>
  </td>
  <td style="padding:10px 14px;width:90px">
    <span style="font-size:12px;color:{bc}">{bl}</span>
  </td>
  <td style="padding:10px 14px;width:120px">
    <span style="font-size:12px;font-weight:700;color:{s_col}">{score}/{MAX}</span>
    {(lambda s16, m16: f'<span style="font-size:11px;color:{"#30d158" if s16 >= 14 else ("#ffd60a" if s16 >= 11 else "#4a5070")};margin-left:6px">| 16pt: {s16}/{m16}</span>')(r.get("score_16pt", 0), r.get("score_16pt_max", 16)) if r.get("score_16pt") is not None else ""}
  </td>
  <td style="padding:10px 14px;width:80px">
    {'<span style="font-size:11px;font-weight:700;background:rgba(255,214,10,0.15);color:#ffd60a;padding:2px 8px;border-radius:3px;letter-spacing:.04em">⬡ ' + str(_gate_count) + '/2</span>' if _gate_count == 1 else ('<span style="font-size:11px;font-weight:700;background:rgba(48,209,88,0.15);color:#30d158;padding:2px 8px;border-radius:3px;letter-spacing:.04em">✓ 2/2</span>' if _gate_count >= 2 else '<span style="font-size:11px;color:#4a5070">—</span>')}
  </td>
  <td style="padding:10px 14px;width:130px">
    <span style="font-size:13px;font-weight:800;color:{buy_c};letter-spacing:.04em">{buy_tag}</span>
  </td>
  <td style="padding:10px 14px;width:70px">
    {vol_html(r.get("vol_ratio"))}
    {(lambda rv: f'<div style="font-size:9px;margin-top:2px;color:{"#ff3b30" if rv>0.40 else ("#ffd60a" if rv>0.25 else "#30d158")}">{rv*100:.0f}% rvol</div>' if rv is not None else "")(mom.get("realized_vol_20d") if (mom := (r.get("momentum") or {})) else None)}
  </td>
  <td style="padding:10px 14px;width:90px">
    <span style="font-size:13px;font-weight:600;color:{mc}">{mv}</span>
  </td>
  <td style="padding:10px 14px;width:24px;text-align:center;color:#b8bdd4;font-size:11px" id="arr-{idx}">▼</td>
</tr>
<tr id="det-{idx}" class="det">
  <td colspan="9" style="padding:0">
    <div style="display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:0;border-top:1px solid #161a28">
      <div style="padding:16px 20px;border-right:1px solid #161a28">
        <div style="font-size:10px;color:#b8bdd4;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">Entry · Stop · Target</div>
        {trade_detail}
      </div>
      <div style="padding:16px 20px;border-right:1px solid #161a28">
        <div style="font-size:10px;color:#b8bdd4;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">Conditions ({sum(1 for k in COND_KEYS if conds.get(k))}/{len(COND_KEYS)} passed)</div>
        {cond_rows}
      </div>
      <div style="padding:16px 20px">
        <div style="font-size:10px;color:#b8bdd4;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">Regime context</div>
        <div style="margin-bottom:8px">
          <div style="font-size:10px;color:#b8bdd4;margin-bottom:2px">Distance from 52-wk high</div>
          <div style="font-size:14px;font-weight:700;color:{pct52_c}">{pct52_str}</div>
        </div>
        <div style="margin-bottom:8px">
          <div style="font-size:10px;color:#b8bdd4;margin-bottom:2px">Realized vol (20d)</div>
          <div style="font-size:14px;font-weight:700;color:{rvol_c}">{rvol_str}</div>
        </div>
        <div>
          <div style="font-size:10px;color:#b8bdd4;margin-bottom:2px">Volume vs 20d avg</div>
          <div style="font-size:14px;font-weight:700">{vol_html(r.get("vol_ratio"))}</div>
        </div>
      </div>
    </div>
  </td>
</tr>"""
    return out


def _reversal_indicator_detail(symbol: str, direction: str) -> str:
    """
    Returns a short string listing which reversal indicators are currently
    firing for an open position (MACD cross and/or EMA 13×30 cross on 15M).
    Used in the active-row expanded detail panel.
    Returns "" on any fetch/compute failure — gracefully degrades.
    """
    try:
        raw_tf  = fetch_multi_timeframe(symbol)
        tf_data = {tf: prepare_df(df) for tf, df in raw_tf.items()}
        df15    = tf_data.get(config.TF_15M)
        if df15 is None:
            return ""
        parts = []
        if direction == "short":
            if macd_bullish_cross(df15, config.MACD_FAST):  parts.append("MACD")
            if ema_bullish_cross(df15):                      parts.append("EMA 13×30")
        else:
            if macd_bearish_cross(df15, config.MACD_FAST):  parts.append("MACD")
            if ema_bearish_cross(df15):                      parts.append("EMA 13×30")
        return " · ".join(parts) if parts else ""
    except Exception as _cs_err:
        logger.debug("[scan] confidence_signals failed — returning '': %s", _cs_err)
        return ""


def build_active_rows(open_trades: dict, results_by_sym: dict, idx_offset: int = 9000) -> str:
    """
    Renders pinned active-position rows at the top of the scanner table.
    open_trades:     tracker.open_trades dict {symbol: trade_dict}
    results_by_sym:  {symbol: scan_result_dict} from current scan cycle
    idx_offset:      row-ID base (9000) avoids collision with watchlist IDs
    Returns "" when no open positions (standalone mode / nothing held).
    """
    if not open_trades:
        return ""

    MAX = sum(config.SCORE_WEIGHTS.values())
    out = (
        '<tr style="background:#060810">'
        '<td colspan="9" style="padding:5px 14px;border-top:2px solid #00e5ff">'
        '<span style="font-size:10px;font-weight:700;color:#00e5ff;'
        'text-transform:uppercase;letter-spacing:.1em">&#9679; Open Positions</span>'
        '</td></tr>'
    )

    for _i, (sym, trade) in enumerate(open_trades.items()):
        idx          = idx_offset + _i
        direction    = trade["direction"]
        entry_price  = trade["entry_price"]
        qty          = trade["qty"]
        active_stop  = trade.get("trail_stop") or trade.get("stop")
        target_price = trade.get("target")
        is_overnight = trade.get("overnight", False)
        is_bucket_a   = sym in getattr(config, "BUCKET_A_TICKERS", set())
        is_orphan     = trade.get("_adopted_orphan", False)
        entry_score   = trade.get("score", 0)
        rev_count     = trade.get("reversal_scan_count", 0)

        # ── Scan result (may be absent if symbol not scanned this cycle) ────
        # Must be assigned before any reference to r (e.g. momentum lookup below)
        r = results_by_sym.get(sym)

        # Momentum for col 6 — same as watchlist row
        _open_mv, _open_mc = mom_val(r.get("momentum") if r else None)

        # ── Current price + unrealized P&L ──────────────────────────────────
        current_price = None
        try:
            from data.alpaca_data import get_latest_quote as _glq, get_latest_trade as _glt
            _q = _glq(sym)
            if _q:
                current_price = (_q["bid"] + _q["ask"]) / 2
            else:
                current_price = _glt(sym)
        except Exception as _apfe:
            logger.warning(f"[{sym}] price fetch failed in build_active_rows — P&L will show as —: {_apfe}")

        if current_price is not None:
            raw_pnl  = ((current_price - entry_price) if direction == "long"
                        else (entry_price - current_price)) * qty
            raw_pct  = ((current_price - entry_price) if direction == "long"
                        else (entry_price - current_price)) / entry_price * 100
            pnl_col  = "#30d158" if raw_pnl >= 0 else "#ff3b30"
            pnl_sign = "+" if raw_pnl >= 0 else ""
            pnl_str  = f'{pnl_sign}${raw_pnl:,.2f} ({raw_pct:+.1f}%)'
        else:
            pnl_col  = "#4a5070"
            pnl_str  = "—"
        # IN POSITION badge — mirrors watchlist format (QQQ-style)
        _badge_pnl = (
            f' <span style="color:{pnl_col};font-weight:700">'
            f'{"+" if raw_pnl >= 0 else ""}${raw_pnl:,.2f}</span>'
        ) if current_price is not None else ""
        _pos_pnl_badge = (
            f'<div style="font-size:9px;margin-top:4px">'
            f'<span style="color:#00e5ff;font-weight:700;letter-spacing:.05em">IN POSITION</span>'
            f'<span style="color:#636680"> · </span>'
            f'<span style="color:#b8bdd4">{qty}sh @ ${entry_price:,.2f}</span>'
            f'{_badge_pnl}</div>'
        )
        if r is not None:
            scan_score = r["short_score"] if direction == "short" else r["long_score"]
        else:
            scan_score = entry_score
        s_col      = sc(scan_score, MAX)
        # Adopted orphans entered manually — show MANUAL rather than a misleading 0/12
        if is_orphan and entry_score == 0:
            score_str = "MANUAL"
            s_col     = "#ff9f0a"   # amber — distinct from red/green scoring colours
        else:
            score_str = f'{scan_score}/{MAX}'

        bl, bc = (BIAS_MAP.get(r.get("daily_bias", ""), ("—", "#4a5070"))
                  if r is not None else ("—", "#4a5070"))

        # ── Badges ──────────────────────────────────────────────────────────
        dir_col  = "#30d158" if direction == "long" else "#ff3b30"
        dir_lbl  = "&#9650; LONG" if direction == "long" else "&#9660; SHORT"

        # Overnight badge: distinguish true AH entry from standard overnight hold.
        # PDT-forced overnight removed — SEC/FINRA rule amendment, board vote S50 28-0.
        _ovnt_label = "OVERNIGHT"
        if is_overnight:
            try:
                _oe_dt = datetime.fromisoformat(trade.get("entry_time", ""))
                if _oe_dt.tzinfo is None:
                    _oe_dt = _oe_dt.replace(tzinfo=PT)
                _oe_et_hour = _oe_dt.astimezone(ET).hour
                if _oe_et_hour >= 16 or _oe_et_hour < 4:
                    _ovnt_label = "OVERNIGHT (AH)"
            except Exception as _ovnt_err:
                logger.debug("[scan] overnight badge parse failed — using default label: %s", _ovnt_err)
        overnight_badge = (
            f'<span style="font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;'
            f'background:rgba(255,159,10,.15);color:#ff9f0a;margin-left:4px">{_ovnt_label}</span>'
        ) if is_overnight else ""

        # Bucket A badge only — Bucket B omitted (the single letter "B" looks like a grade)
        bucket_badge = (
            '<span style="font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;'
            'background:rgba(10,132,255,.15);color:#0a84ff">BUCKET A</span>'
        ) if is_bucket_a else ""

        # H-4: Stop-breach-blocked badge — fires when stop was breached but PDT
        # or Bucket A same-day rule blocked the close. Visible immediately on
        # the scanner without requiring log inspection.
        # H-4 fix: suppress badge if current price has recovered inside the stop
        # (stop_breached flag is never cleared from the trade dict — it is an
        # audit trail, not live state. Display layer must check current price.)
        _stop_breached = trade.get("stop_breached", False)
        _breach_price  = trade.get("stop_breach_price")
        _breach_recovered = (
            current_price is not None and active_stop is not None and (
                (direction == "long"  and current_price > active_stop) or
                (direction == "short" and current_price < active_stop)
            )
        ) if _stop_breached else False
        if _stop_breached and not _breach_recovered:
            _bp_str = f"${_breach_price:,.2f}" if _breach_price else "—"
            stop_breach_badge = (
                f'<span style="font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;'
                f'background:rgba(255,59,48,.2);color:#ff3b30;margin-left:4px">'
                f'&#9888; STOP BREACHED {_bp_str}</span>'
            )
        else:
            stop_breach_badge = ""

        if rev_count > 0:
            _scan_min  = (config.RTH_REVERSAL_SCAN_MIN if not is_overnight
                          else config.OVERNIGHT_REVERSAL_SCAN_MIN)
            rev_col    = "#ffd60a" if rev_count < _scan_min else "#ff3b30"
            _ind_str   = _reversal_indicator_detail(sym, direction)
            _ind_tag   = (f' · <span style="color:#e2e4ee">{_ind_str}</span>'
                          if _ind_str else "")
            _close_tag = (' · <span style="color:#ff3b30;font-weight:700">SELL PENDING</span>'
                          if rev_count >= _scan_min else "")
            rev_badge = (
                f'<span style="font-size:9px;padding:1px 5px;border-radius:3px;'
                f'background:rgba(255,159,10,.1);color:{rev_col};margin-left:4px">'
                f'REV {rev_count}/{_scan_min}</span>'
            )
            rev_detail = (
                f'Reversal: <span style="color:{rev_col};font-weight:700">'
                f'{rev_count}/{_scan_min} scans</span>{_ind_tag}{_close_tag}'
            )
        else:
            rev_badge  = ""
            rev_detail = "No reversal signal"

        # ── Stop / target detail strings ─────────────────────────────────────
        if active_stop and entry_price:
            stop_pct_str = f'{abs(entry_price - active_stop) / entry_price * 100:.1f}%'
            stop_detail  = f'${active_stop:,.2f} ({stop_pct_str})'
        else:
            stop_detail = "—"

        tgt_str       = f'${target_price:,.2f}' if target_price else '—'
        cur_price_str = f'${current_price:,.2f}' if current_price else '—'

        entry_time = trade.get("entry_time", "")
        try:
            _et_dt = datetime.fromisoformat(entry_time)
            if _et_dt.tzinfo is None:
                _et_dt = _et_dt.replace(tzinfo=PT)   # naive = stored in local PT
            et_fmt = _et_dt.astimezone(PT).strftime("%b %-d %-I:%M %p PT")
        except Exception as _etfmt_err:
            logger.debug("[scan] entry_time display format failed — using '—': %s", _etfmt_err)
            et_fmt = "—"

        qty_rem      = trade.get("qty_remaining", qty)
        partial_str  = (f' &nbsp;&#183;&nbsp; Remaining: <span style="color:#e2e4ee">{qty_rem}</span>'
                        if qty_rem != qty else "")
        ovnt_inline  = (f"&nbsp;&#183;&nbsp; <span style='color:#ff9f0a;font-weight:600'>{_ovnt_label}</span>"
                        if is_overnight else "")
        if is_orphan and entry_score == 0:
            scan_src = (f'Live score: <span style="color:{s_col};font-weight:700">{scan_score}/{MAX}</span><br>'
                        if r is not None
                        else '<span style="color:#ff9f0a;font-weight:700">MANUAL entry — no bot score</span><br>')
        else:
            scan_src = (f'Live score: <span style="color:{s_col};font-weight:700">{scan_score}/{MAX}</span><br>'
                        if r is not None else "Not in current scan<br>")

        out += f"""<tr id="row-{idx}" class="mrow" onclick="tog({idx})" style="background:rgba(0,229,255,0.04);box-shadow:inset 3px 0 0 #00e5ff">
  <td style="padding:10px 14px;width:160px">
    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
      <span style="font-size:9px;font-weight:700;padding:1px 6px;border-radius:3px;
        background:rgba(0,229,255,.2);color:#00e5ff;letter-spacing:.05em">ACTIVE</span>
      <span style="font-weight:700;font-size:14px;color:#00e5ff;letter-spacing:.02em">{sym}</span>
      {bucket_badge}
    </div>
    <div style="margin-top:3px">{overnight_badge}{rev_badge}{stop_breach_badge}</div>
    {_pos_pnl_badge}
  </td>
  <td style="padding:10px 14px;width:110px">
    <span style="font-size:13px;font-weight:700;color:#e2e4ee">{cur_price_str}</span>
  </td>
  <td style="padding:10px 14px;width:90px">
    <span style="font-size:12px;color:{bc}">{bl}</span>
  </td>
  <td style="padding:10px 14px;width:120px">
    <span style="font-size:12px;font-weight:700;color:{s_col}">{score_str}</span>
    {(lambda s16, m16: f'<span style="font-size:11px;color:{"#30d158" if s16 >= 14 else ("#ffd60a" if s16 >= 11 else "#4a5070")};margin-left:6px">| 16pt: {s16}/{m16}</span>')(r.get("score_16pt", 0), r.get("score_16pt_max", 16)) if r is not None and r.get("score_16pt") is not None else ""}
  </td>
  <td style="padding:10px 14px;width:80px">
    <span style="font-size:11px;color:#4a5070">—</span>
  </td>
  <td style="padding:10px 14px;width:130px">
    {(lambda lbl, col: f'<span style="font-size:13px;font-weight:800;color:{col};letter-spacing:.04em">{lbl}</span>')(*decision_tag(r)) if r is not None else f'<span style="font-size:13px;font-weight:800;color:{dir_col}">{dir_lbl}</span>'}
  </td>
  <td style="padding:10px 14px;width:70px">
    {vol_html(r.get("vol_ratio") if r is not None else None)}
  </td>
  <td style="padding:10px 14px;width:90px">
    <span style="font-size:13px;font-weight:600;color:{_open_mc}">{_open_mv}</span>
  </td>
  <td style="padding:10px 14px;width:24px;text-align:center;color:#b8bdd4;font-size:11px"
    id="arr-{idx}">&#9660;</td>
</tr>
<tr id="det-{idx}" class="det">
  <td colspan="9" style="padding:0">
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;border-top:1px solid #1a2a2a">
      <div style="padding:16px 20px;border-right:1px solid #1a2a2a">
        <div style="font-size:10px;color:#b8bdd4;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">Position Details</div>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px">
          <div>
            <div style="font-size:10px;color:#b8bdd4;margin-bottom:2px">Entry</div>
            <div style="font-size:14px;font-weight:700;color:#e2e4ee">${entry_price:,.2f}</div>
          </div>
          <div>
            <div style="font-size:10px;color:#b8bdd4;margin-bottom:2px">Stop</div>
            <div style="font-size:14px;font-weight:700;color:#ff3b30">{stop_detail}</div>
          </div>
          <div>
            <div style="font-size:10px;color:#b8bdd4;margin-bottom:2px">Target</div>
            <div style="font-size:14px;font-weight:700;color:#30d158">{tgt_str}</div>
          </div>
        </div>
        <div style="font-size:11px;color:#b8bdd4">Qty: <span style="color:#e2e4ee">{qty}</span>{partial_str} &nbsp;&#183;&nbsp; Entered: <span style="color:#e2e4ee">{et_fmt}</span></div>
      </div>
      <div style="padding:16px 20px;border-right:1px solid #1a2a2a">
        <div style="font-size:10px;color:#b8bdd4;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">Unrealized P&amp;L</div>
        <div style="font-size:22px;font-weight:800;color:{pnl_col};margin-bottom:8px">{pnl_str}</div>
        <div style="font-size:11px;color:#b8bdd4">Direction: <span style="color:{dir_col};font-weight:700">{direction.upper()}</span> &nbsp;&#183;&nbsp; Mode: <span style="color:#e2e4ee">{trade.get("trade_mode","—").upper()}</span>{ovnt_inline}</div>
      </div>
      <div style="padding:16px 20px">
        <div style="font-size:10px;color:#b8bdd4;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">Scan Status</div>
        <div style="font-size:11px;color:#c8ccd8;line-height:1.8">Entry score: <span style="color:#e2e4ee;font-weight:700">{entry_score}/{MAX}</span><br>{scan_src}{rev_detail}</div>
      </div>
    </div>
  </td>
</tr>"""

    out += (
        '<tr style="background:#060810">'
        '<td colspan="9" style="padding:4px 14px;border-top:1px solid #0e1020;border-bottom:1px solid #161a28">'
        '<span style="font-size:10px;color:#2a3050;text-transform:uppercase;letter-spacing:.08em">Watchlist</span>'
        '</td></tr>'
    )
    return out


def _fetch_spy_0dte_data() -> "dict | None":
    """
    Fetch SPY 0DTE options chain and build IV surface for directional strike selection.
    Uses Black-Scholes delta per strike to find OTM options targeting delta ~0.30-0.40.
    No longer walks outward from ATM by OI — selects directionally appropriate OTM strike.
    Returns full surface + selected strikes. None on failure.
    """
    import math
    try:
        import yfinance as yf

        def _norm_cdf(x):
            """Standard normal CDF via math.erf — no scipy needed."""
            return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

        def _bs_delta(S, K, T, r, sigma, opt_type="call"):
            """Black-Scholes delta. T in years. Returns None on invalid inputs."""
            if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
                return None
            try:
                d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
                return _norm_cdf(d1) if opt_type == "call" else _norm_cdf(d1) - 1.0
            except Exception as _bsd_e:
                logger.debug("_bs_delta: delta calc failed — %s", _bsd_e)
                return None

        ticker = yf.Ticker("SPY")
        price  = float(ticker.fast_info.last_price or 0)
        if price <= 0:
            return None

        expirations = ticker.options
        if not expirations:
            return None

        # P5-L2: weekday filter prevents phantom Sat/Sun expiry entries from yfinance
        today  = datetime.now(ET).date()
        future = [e for e in expirations
                  if datetime.strptime(e, "%Y-%m-%d").date() >= today
                  and datetime.strptime(e, "%Y-%m-%d").weekday() < 5]   # Mon–Fri only
        if not future:
            return None
        nearest = future[0]

        chain = ticker.option_chain(nearest)
        calls = chain.calls.copy()
        puts  = chain.puts.copy()

        if calls.empty or puts.empty:
            return None

        # P/C OI ratio across full chain
        total_call_oi = float(calls["openInterest"].fillna(0).sum())
        total_put_oi  = float(puts["openInterest"].fillna(0).sum())
        pc_ratio = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else None

        # ATM straddle for implied range + skew wing distance
        calls["_diff"] = abs(calls["strike"] - price)
        puts["_diff"]  = abs(puts["strike"]  - price)
        ac = calls.nsmallest(1, "_diff").iloc[0]
        ap = puts.nsmallest(1,  "_diff").iloc[0]
        call_mid_atm = (float(ac["bid"]) + float(ac["ask"])) / 2 if float(ac.get("bid", 0)) > 0 else float(ac["lastPrice"])
        put_mid_atm  = (float(ap["bid"]) + float(ap["ask"])) / 2 if float(ap.get("bid", 0)) > 0 else float(ap["lastPrice"])
        straddle = call_mid_atm + put_mid_atm

        # Time to close — floor at 15 min to avoid BS singularity near T=0
        now_et    = datetime.now(ET)
        close_et  = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        hours_rem = max(0.25, (close_et - now_et).total_seconds() / 3600)
        T_years   = hours_rem / 8760.0   # hours / (365 * 24)
        r         = 0.05                 # risk-free rate approximation

        # ── Build IV surface with BS delta per strike ─────────────────────────
        def _build_surface(df, opt_type):
            rows = []
            for _, row in df.iterrows():
                K    = float(row["strike"])
                iv   = float(row.get("impliedVolatility", 0) or 0)
                _oi  = row.get("openInterest", 0)
                oi   = int(_oi) if _oi and not (isinstance(_oi, float) and math.isnan(_oi)) else 0
                bid  = float(row.get("bid", 0) or 0)
                ask  = float(row.get("ask", 0) or 0)
                last = float(row.get("lastPrice", 0) or 0)
                mid  = (bid + ask) / 2 if bid > 0 and ask > 0 else last
                delta = _bs_delta(price, K, T_years, r, iv, opt_type) if iv > 0 else None
                rows.append({
                    "strike": K,
                    "iv":     round(iv * 100, 1),
                    "delta":  delta,
                    "oi":     oi,
                    "mid":    round(mid, 2),
                    "bid":    bid,
                    "ask":    ask,
                })
            return sorted(rows, key=lambda x: x["strike"])

        call_surface = _build_surface(calls, "call")
        put_surface  = _build_surface(puts,  "put")

        # ── OTM strike selection: target delta ~0.35 ─────────────────────────
        DELTA_TARGET = 0.35
        DELTA_MIN    = 0.20   # too deep OTM → illiquid / lottery
        DELTA_MAX    = 0.48   # too close to ATM → overpaying for theta
        MIN_OI       = 200    # OTM strikes have less OI than ATM

        def _select_otm_call(surface, spot):
            """Select OTM call closest to delta=0.35, OI >= MIN_OI."""
            candidates = [r for r in surface
                          if r["strike"] > spot
                          and r["delta"] is not None
                          and DELTA_MIN <= r["delta"] <= DELTA_MAX
                          and r["oi"] >= MIN_OI
                          and r["iv"] > 0]
            if candidates:
                return min(candidates, key=lambda x: abs(x["delta"] - DELTA_TARGET))
            # Fallback: any OTM with delta and OI
            fallback = [r for r in surface
                        if r["strike"] > spot and r["delta"] is not None and r["oi"] > 0]
            if fallback:
                return min(fallback, key=lambda x: abs(x["delta"] - DELTA_TARGET))
            return min(surface, key=lambda x: abs(x["strike"] - spot))  # last resort: ATM

        def _select_otm_put(surface, spot):
            """Select OTM put closest to abs(delta)=0.35, OI >= MIN_OI."""
            candidates = [r for r in surface
                          if r["strike"] < spot
                          and r["delta"] is not None
                          and -DELTA_MAX <= r["delta"] <= -DELTA_MIN
                          and r["oi"] >= MIN_OI
                          and r["iv"] > 0]
            if candidates:
                return min(candidates, key=lambda x: abs(x["delta"] - (-DELTA_TARGET)))
            fallback = [r for r in surface
                        if r["strike"] < spot and r["delta"] is not None and r["oi"] > 0]
            if fallback:
                return min(fallback, key=lambda x: abs(x["delta"] - (-DELTA_TARGET)))
            return min(surface, key=lambda x: abs(x["strike"] - spot))

        call_sel = _select_otm_call(call_surface, price)
        put_sel  = _select_otm_put(put_surface, price)

        # ── IV skew: put wing IV − call wing IV at ±1 straddle distance ──────
        # Positive skew = put vol rich vs call vol (normal in equities)
        call_wing = min(call_surface, key=lambda x: abs(x["strike"] - (price + straddle)))
        put_wing  = min(put_surface,  key=lambda x: abs(x["strike"] - (price - straddle)))
        cw_iv = call_wing["iv"] if call_wing else None
        pw_iv = put_wing["iv"]  if put_wing  else None
        iv_skew = round(pw_iv - cw_iv, 1) if (cw_iv and pw_iv and cw_iv > 0) else None

        # ── Sigma levels ──────────────────────────────────────────────────────
        sigma_1_low  = round(price - straddle, 2)
        sigma_1_high = round(price + straddle, 2)
        sigma_2_low  = round(price - 2 * straddle, 2)
        sigma_2_high = round(price + 2 * straddle, 2)

        # ── Fib retracements + prior day S/R (20-day daily bars) ─────────────
        fib_levels = {}
        try:
            _hist = ticker.history(period="21d", interval="1d")
            if not _hist.empty and len(_hist) >= 2:
                _swing_high = float(_hist["High"].max())
                _swing_low  = float(_hist["Low"].min())
                _prior_high = float(_hist["High"].iloc[-2])
                _prior_low  = float(_hist["Low"].iloc[-2])
                _rng = _swing_high - _swing_low
                if _rng > 0:
                    fib_levels = {
                        "swing_high":  round(_swing_high, 2),
                        "swing_low":   round(_swing_low, 2),
                        "prior_high":  round(_prior_high, 2),
                        "prior_low":   round(_prior_low, 2),
                        "fib_786":     round(_swing_high - 0.214 * _rng, 2),
                        "fib_618":     round(_swing_high - 0.382 * _rng, 2),
                        "fib_500":     round(_swing_high - 0.500 * _rng, 2),
                        "fib_382":     round(_swing_high - 0.618 * _rng, 2),
                        "fib_236":     round(_swing_high - 0.764 * _rng, 2),
                    }
        except Exception as _fibe:
            logger.warning(f"0DTE fib/sigma/S&R computation failed — levels will be absent this cycle: {_fibe}")

        return {
            "price":         price,
            "expiry":        nearest,
            "straddle":      straddle,
            "move_pct":      round(straddle / price * 100, 2) if price > 0 else None,
            "range_low":     round(price - straddle, 2),
            "range_high":    round(price + straddle, 2),
            "pc_ratio":      pc_ratio,
            "hours_rem":     hours_rem,
            # Delta-selected OTM call
            "call_strike":   call_sel["strike"],
            "call_iv":       call_sel["iv"],
            "call_oi":       call_sel["oi"],
            "call_delta":    round(call_sel["delta"], 3) if call_sel["delta"] is not None else None,
            "call_mid":      call_sel["mid"],
            # Delta-selected OTM put
            "put_strike":    put_sel["strike"],
            "put_iv":        put_sel["iv"],
            "put_oi":        put_sel["oi"],
            "put_delta":     round(put_sel["delta"], 3) if put_sel["delta"] is not None else None,
            "put_mid":       put_sel["mid"],
            # IV skew across 1-sigma wing
            "iv_skew":       iv_skew,
            # Full surfaces for downstream validation
            "call_surface":  call_surface,
            "put_surface":   put_surface,
            # Sigma levels
            "sigma_1_low":   sigma_1_low,
            "sigma_1_high":  sigma_1_high,
            "sigma_2_low":   sigma_2_low,
            "sigma_2_high":  sigma_2_high,
            "fib_levels":    fib_levels,
        }
    except Exception as _opt_err:
        logger.warning("[scan] _fetch_options_data fetch failed — returning None: %s", _opt_err)
        return None


def _fetch_yfinance_news() -> list:
    """
    Fetch recent market news via yfinance for SPY + major tickers.
    Writes logs/market_news.json and returns list of items.
    Items: [{"headline": str, "time": str, "source": str}]
    Only last 12 hours; deduped; capped at 20 items.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    try:
        import yfinance as _yf
    except ImportError:
        return []

    PDT_TZ   = ZoneInfo("America/Los_Angeles")
    items    = []
    seen     = set()
    now_utc  = _dt.now(_tz.utc)

    for sym in ["SPY", "QQQ", "AAPL", "NVDA", "MSFT", "TSLA"]:
        try:
            news = _yf.Ticker(sym).news or []
            for n in news[:6]:
                title = (n.get("content", {}).get("title") or
                         n.get("title") or n.get("headline") or "")
                title = title.strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                ts = (n.get("content", {}).get("pubDate") or
                      n.get("providerPublishTime") or
                      n.get("publishedAt") or 0)
                if isinstance(ts, str):
                    try:
                        pub_dt = _dt.fromisoformat(ts.replace("Z", "+00:00"))
                    except Exception as _pdt_e:
                        logger.debug(
                            "_fetch_yfinance_news: pub_dt parse failed — %s", _pdt_e
                        )
                        pub_dt = None
                elif ts:
                    pub_dt = _dt.fromtimestamp(int(ts), tz=_tz.utc)
                else:
                    pub_dt = None
                if pub_dt:
                    age_hrs = (now_utc - pub_dt).total_seconds() / 3600
                    if age_hrs > 12:
                        continue
                    time_str = pub_dt.astimezone(PDT_TZ).strftime("%-I:%M %p PT")
                else:
                    time_str = "—"
                publisher = (n.get("content", {}).get("provider", {}).get("displayName") or
                             n.get("publisher") or sym)
                items.append({"headline": title, "time": time_str, "source": publisher})
        except Exception as _art_e:
            logger.debug("_fetch_yfinance_news: article parse failed — %s", _art_e)
            continue
        if len(items) >= 20:
            break

    items = items[:20]

    # Write to logs/market_news.json atomically
    try:
        import os as _os
        logs_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "logs")
        out_path = _os.path.join(logs_dir, "market_news.json")
        tmp_path = out_path + ".tmp"
        with open(tmp_path, "w") as _f:
            _json.dump({"generated": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "items": items}, _f)
            _f.flush()
            _os.fsync(_f.fileno())
        _os.replace(tmp_path, out_path)
    except Exception as _mce:
        logger.warning(f"Movers/news cache write failed — next scan will re-fetch: {_mce}")

    return items


def _load_dte_prev() -> dict:
    """Load previous 0DTE recommendation from logs/dte_prev.json. Returns {} on missing/error."""
    try:
        import json as _j
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "dte_prev.json")
        with open(_p) as _f:
            return _j.load(_f)
    except FileNotFoundError:
        return {}  # expected on first run — silent
    except Exception as _ldp_e:
        logger.debug("_load_dte_prev: load failed — %s", _ldp_e)
        return {}


def _save_dte_prev(rec: dict, confirm_count: int = 1) -> None:
    """Atomically write current 0DTE rec to logs/dte_prev.json for next-cycle comparison.
    TB-5: confirm_count tracks consecutive scans with same direction (phantom flip guard).
    """
    try:
        import json as _j
        _p   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "dte_prev.json")
        _tmp = _p + ".tmp"
        _payload = {
            "direction":     rec.get("direction"),
            "strike":        rec.get("strike"),
            "size":          rec.get("size"),
            "ts":            datetime.now(ET).isoformat(),
            "confirm_count": confirm_count,   # TB-5: scans this direction has persisted
        }
        with open(_tmp, "w") as _f:
            _j.dump(_payload, _f)
            _f.flush()
            os.fsync(_f.fileno())
        os.replace(_tmp, _p)
    except Exception as _pae:
        logger.warning(f"Pending alerts save failed — alert state may reset on restart: {_pae}")


def _compute_0dte_rec(spy_result, chain_data, vix_val, cr):
    """
    Compute 0DTE SPY OTM strike recommendation using IV surface and delta targeting.
    Strike: delta-selected OTM (~0.35 target) — not nearest ATM by OI proximity.
    Size: most conservative of VIX tier, IV tier, theta cap, IV skew tier.
    Accuracy improvements:
      - Time filter: no rec in first 30 min of RTH (9:30–10:00 ET) — choppy open
      - Stickiness: direction flip requires CONVICTION_FULL_MIN score, prevents whipsaw
      - VWAP filter: direction must agree with SPY vs VWAP
      - Bias label: surfaces the raw daily_bias string for display
    Returns dict with no_rec=True when insufficient data or no directional read.
    """
    if not chain_data or not spy_result:
        return {"no_rec": True}

    # ── Time filter: block first 30 min of RTH (9:30–10:00 ET) ──────────────
    _now_et = datetime.now(ET)
    _mins   = _now_et.hour * 60 + _now_et.minute
    if _now_et.weekday() < 5 and (9 * 60 + 30) <= _mins < (10 * 60):
        return {"no_rec": True, "no_rec_reason": "Opening 30 min — too choppy for 0DTE"}

    bias      = spy_result.get("daily_bias", "unknown")
    long_sig  = spy_result.get("long_signal", False)
    short_sig = spy_result.get("short_signal", False)
    spy_score = max(spy_result.get("long_score", 0), spy_result.get("short_score", 0))
    cr_regime = (cr or {}).get("composite_regime", "NEUTRAL")
    if cr_regime == "HIGH_VOL":
        cr_regime = "NEUTRAL"

    # Raw direction from bias/signal
    if long_sig or bias in ("bull", "strong_bull"):
        raw_direction = "CALL"
    elif short_sig or bias in ("bear", "strong_bear"):
        raw_direction = "PUT"
    elif bias == "neutral":
        if cr_regime == "BULL":
            raw_direction = "CALL"
        elif cr_regime == "BEAR":
            raw_direction = "PUT"
        else:
            return {"no_rec": True}
    else:
        return {"no_rec": True}

    # ── Stickiness: require FULL conviction (≥11/12) to flip direction ───────
    prev       = _load_dte_prev()
    prev_dir   = prev.get("direction")
    direction  = raw_direction
    _stickiness_locked = False
    if prev_dir and prev_dir != raw_direction:
        if spy_score < config.CONVICTION_FULL_MIN:
            direction = prev_dir   # hold previous direction — insufficient conviction to flip
            _stickiness_locked = True   # VWAP cannot override a stickiness-protected direction

    # ── VWAP filter: 1-min yfinance SPY for real-time direction ─────────────
    # VWAP is a confirmation signal, NOT an unconditional override.
    # Rules: (1) blocked when stickiness guard is active — prevents double-whipsaw;
    #        (2) SPY must be >0.10% away from VWAP — filters noise hugging VWAP midline.
    try:
        _spy_1m = fetch_bars("SPY", config.TF_1M, num_bars=400)  # T1 — Alpaca Data API; ~390 bars = full RTH day
        if not _spy_1m.empty and len(_spy_1m) >= 5 and not _stickiness_locked:
            _tp  = (_spy_1m["high"] + _spy_1m["low"] + _spy_1m["close"]) / 3
            _vwap_series = (_tp * _spy_1m["volume"]).cumsum() / _spy_1m["volume"].cumsum()
            _cur_vwap  = float(_vwap_series.iloc[-1].item() if hasattr(_vwap_series.iloc[-1], "item") else _vwap_series.iloc[-1])
            _cur_price = float(_spy_1m["close"].iloc[-1].item() if hasattr(_spy_1m["close"].iloc[-1], "item") else _spy_1m["close"].iloc[-1])
            _vwap_separation = abs(_cur_price - _cur_vwap) / _cur_vwap if _cur_vwap > 0 else 0
            if _vwap_separation > 0.001:   # >0.10% separation required — meaningful VWAP signal
                if direction == "CALL" and _cur_price < _cur_vwap:
                    direction = "PUT"
                elif direction == "PUT" and _cur_price > _cur_vwap:
                    direction = "CALL"
    except Exception as _vwape:
        logger.warning(f"0DTE 1-min VWAP fetch failed — proceeding without VWAP direction override: {_vwape}")

    price = chain_data["price"]

    # Strike, IV, delta, OI — OTM selection from IV surface
    if direction == "CALL":
        strike = chain_data["call_strike"]
        iv     = chain_data["call_iv"]
        oi     = chain_data["call_oi"]
        delta  = chain_data.get("call_delta")
        mid    = chain_data.get("call_mid")
    else:
        strike = chain_data["put_strike"]
        iv     = chain_data["put_iv"]
        oi     = chain_data["put_oi"]
        delta  = chain_data.get("put_delta")
        mid    = chain_data.get("put_mid")

    # ── Find nearest significant level to the selected strike ────────────────
    _fib  = chain_data.get("fib_levels", {})
    _sig_candidates = {
        "1σ " + ("low"  if direction == "PUT" else "high"): (
            chain_data.get("sigma_1_low") if direction == "PUT"
            else chain_data.get("sigma_1_high")),
        "2σ " + ("low"  if direction == "PUT" else "high"): (
            chain_data.get("sigma_2_low") if direction == "PUT"
            else chain_data.get("sigma_2_high")),
        "prior day high": _fib.get("prior_high"),
        "prior day low":  _fib.get("prior_low"),
        "Fib 61.8%":      _fib.get("fib_618"),
        "Fib 50.0%":      _fib.get("fib_500"),
        "Fib 38.2%":      _fib.get("fib_382"),
        "Fib 78.6%":      _fib.get("fib_786"),
        "Fib 23.6%":      _fib.get("fib_236"),
    }
    _nearest_name = None
    _nearest_dist = float("inf")
    for _lname, _lvl in _sig_candidates.items():
        if _lvl is not None:
            _d = abs(strike - _lvl)
            if _d < _nearest_dist and _d <= 4.0:
                _nearest_dist = _d
                _nearest_name = f"{_lname} ${_lvl:.2f}"
    nearest_level = _nearest_name

    otm_pct = round(abs(strike - price) / price * 100, 2) if price > 0 else None

    # Size — VIX tier
    if vix_val is None or vix_val < 18:
        vix_tier = 3
    elif vix_val < 23:
        vix_tier = 2
    else:
        vix_tier = 1

    # Size — option IV tier (expensive premium reduces size)
    if iv < 25:
        iv_tier = 3
    elif iv < 35:
        iv_tier = 2
    else:
        iv_tier = 1

    # Theta cap: < 2 hrs to close → max 1/4 (premium decays too fast near expiry)
    theta_cap = 1 if chain_data.get("hours_rem", 6.5) < 2.0 else 3

    # IV skew tier: reduce size if trading into the rich vol side
    skew_val  = chain_data.get("iv_skew")
    skew_tier = 3
    if skew_val is not None:
        if direction == "PUT" and skew_val > 5:
            skew_tier = 2
        elif direction == "CALL" and skew_val < -5:
            skew_tier = 2

    size = {3: "3", 2: "2", 1: "1"}[min(vix_tier, iv_tier, theta_cap, skew_tier)]

    # Actionable size reason — explain WHY size was reduced
    _binding = min(vix_tier, iv_tier, theta_cap, skew_tier)
    if _binding == theta_cap and theta_cap < 3:
        size_reason = "< 2h to close — theta decay risk"
    elif _binding == iv_tier and iv_tier < 3:
        size_reason = f"IV {iv:.0f}% — premium elevated"
    elif _binding == vix_tier and vix_tier < 3 and vix_val is not None:
        size_reason = f"VIX {vix_val:.1f} — vol elevated"
    elif _binding == skew_tier and skew_tier < 3:
        size_reason = "trading into rich vol side"
    else:
        size_reason = "conditions favorable — full size"
    _contracts_str = "1 CONTRACT" if size == "1" else f"{size} CONTRACTS"

    # P/C OI label
    pc = chain_data.get("pc_ratio")
    if pc is not None:
        if pc > 1.2:
            pc_label = f"{pc:.2f} bearish lean"
        elif pc < 0.8:
            pc_label = f"{pc:.2f} bullish lean"
        else:
            pc_label = f"{pc:.2f} neutral"
    else:
        pc_label = "—"

    # IV skew label
    if skew_val is not None:
        if skew_val > 3:
            skew_label = f"+{skew_val:.1f}% put rich"
        elif skew_val < -3:
            skew_label = f"{skew_val:.1f}% call rich"
        else:
            skew_label = f"{skew_val:+.1f}% neutral"
    else:
        skew_label = "—"

    # Bias display label (e.g. "strong_bear" → "strong bear")
    _bias_display = bias.replace("_", " ") if bias and bias != "unknown" else "—"

    rec = {
        "no_rec":       False,
        "strike":       strike,
        "direction":    direction,
        "size":         size,
        "iv":           iv,
        "oi":           oi,
        "delta":        delta,
        "mid":          mid,
        "otm_pct":      otm_pct,
        "pc_label":     pc_label,
        "skew_label":   skew_label,
        "iv_skew":      skew_val,
        "move_pct":     chain_data.get("move_pct"),
        "range_low":    chain_data.get("range_low"),
        "range_high":   chain_data.get("range_high"),
        "expiry":       chain_data["expiry"],
        "bias":         bias,
        "bias_display": _bias_display,
        "spy_score":    spy_score,
        "size_reason":   size_reason,
        "vix_val":       vix_val,
        "contracts_str": _contracts_str,
        "hours_rem":     chain_data.get("hours_rem"),
        "nearest_level": nearest_level,
        "sigma_1_low":   chain_data.get("sigma_1_low"),
        "sigma_1_high":  chain_data.get("sigma_1_high"),
        "sigma_2_low":   chain_data.get("sigma_2_low"),
        "sigma_2_high":  chain_data.get("sigma_2_high"),
        "fib_levels":    chain_data.get("fib_levels", {}),
    }
    _save_dte_prev(rec)
    return rec


def write_html(data):
    results   = data["results"]
    scan_time = data["scan_time"]
    open_now  = data["market_open"]
    MAX       = sum(config.SCORE_WEIGHTS.values())
    refresh   = 60   # 1min — keeps page in sync with ~5min bot cycles

    # ── Timezone conversion: scan_time is ET naive or aware → convert to PT ──
    # scan_time is logged in ET (naive). Replace tzinfo=ET then convert to PT.
    _PST = ZoneInfo("America/Los_Angeles")
    try:
        _scan_dt = datetime.fromisoformat(scan_time)
        if _scan_dt.tzinfo is None:
            _scan_dt = _scan_dt.replace(tzinfo=ET)   # assume ET if no tz
        _scan_pst     = _scan_dt.astimezone(_PST)
        _hr     = str(int(_scan_pst.strftime("%I")))  # strip leading zero
        pt_time = (_scan_pst.strftime("%b %d · ")
                   + _hr + _scan_pst.strftime(":%M %p ")
                   + "PT")
    except Exception as _ptconv_err:
        logger.debug("[scan] scan_time PT conversion failed — showing raw ISO: %s", _ptconv_err)
        pt_time = scan_time  # conversion failed — show raw ISO string as last resort

    # ── Next market open for closed pill ─────────────────────────────────────
    _now_et = datetime.now(ET)
    if not open_now:
        try:
            _d = 1
            while True:
                _cand = _now_et + timedelta(days=_d)
                if _cand.weekday() < 5:
                    _next_et  = _cand.replace(hour=9, minute=30, second=0, microsecond=0)
                    break
                _d += 1
            _next_pst   = _next_et.astimezone(_PST)
            _next_hr    = str(int(_next_pst.strftime("%I")))
            next_open_str = _next_hr + _next_pst.strftime(":%M %p PT")
        except Exception as _nxt_err:
            logger.debug("[scan] next market open calc failed — using '6:30 AM PT': %s", _nxt_err)
            next_open_str = "6:30 AM PT"
    else:
        next_open_str = ""

    pill_class = "open"   if open_now else "closed"
    pill_text  = "MARKET OPEN" if open_now else f"CLOSED · OPENS {next_open_str}"

    # ── Portfolio value + variable watchlist (pre-market movers) ────────────
    _pv      = data.get("portfolio_value")
    pv_str   = f" · Portfolio: ${_pv:,.2f}" if _pv else ""
    _pm_extra = set(data.get("pm_extra") or [])
    _pm_all   = set(data.get("pm_all")   or [])

    PINNED      = ["SPY", "QQQ"]
    open_trades  = data.get("open_trades") or {}
    confirm_gate = data.get("confirm_gate") or {}
    active_syms = set(open_trades.keys()) - set(PINNED)   # SPY/QQQ keep normal pinned slot
    pinned      = [r for r in results if r["symbol"] in PINNED]
    pinned.sort(key=lambda r: PINNED.index(r["symbol"]))
    rest        = [r for r in results if r["symbol"] not in PINNED and r["symbol"] not in active_syms]
    sorted_rest = sorted(rest, key=lambda r: max(r["long_score"], r["short_score"]), reverse=True)
    longs   = [r["symbol"] for r in results if r["long_signal"]]
    shorts  = [r["symbol"] for r in results if r["short_signal"]]
    trades  = [r["symbol"] for r in results
               if (r["long_signal"] or r["short_signal"])
               and max(r["long_score"],r["short_score"])/MAX >= .75
               and (r.get("vol_ratio") or 0) >= 1.2]

    # Market regime — % of tickers with long score >= 75%
    bull_count  = sum(1 for r in results if r["long_score"]/MAX >= .75)
    bear_count  = sum(1 for r in results if r["short_score"]/MAX >= .75)
    bull_pct    = int(bull_count / len(results) * 100) if results else 0
    if bull_pct >= 60:
        regime_label, regime_col = "BULL REGIME", "#30d158"
        regime_sub = f"{bull_count}/{len(results)} tickers bullish setup"
    elif bull_pct >= 35:
        regime_label, regime_col = "MIXED", "#ffd60a"
        regime_sub = "choppy — wait for cleaner setups"
    else:
        regime_label, regime_col = "BEAR / RISK-OFF", "#ff3b30"
        regime_sub = f"{bear_count}/{len(results)} tickers bearish setup"

    # Use active config values — main.py applies --profile overrides before calling write_scan_html
    sm = config.INTRADAY_STOP_ATR_MULT
    tm = config.INTRADAY_TARGET_ATR_MULT
    mn = config.MIN_LONG_SCORE

    # ── Config-driven threshold labels for footer and empty states ───────────
    _full = config.CONVICTION_FULL_MIN       # 11
    _half = config.CONVICTION_HALF_MIN       # 9
    _skip = config.CONVICTION_SKIP_BELOW     # 9
    _trade_min_score = int(MAX * 0.75)       # 9 at 12pt
    trade_empty_lbl  = f"score≥{_trade_min_score}/{MAX} + vol≥1.2×"
    footer_legend = (
        f"LONG/SHORT (FULL) = {_full}–{MAX}/{MAX} full size · "
        f"LONG/SHORT (½) = {_half}–{_full-1}/{MAX} half size · "
        f"SKIP = below {_skip}/{MAX} or no signal · "
        f"HOLD = Bucket A leveraged ETF (min 1-day hold) · "
        f"WK BEAR/BULL SKIP = bot found signal but weekly trend opposes — review manually · "
        f"Bucket A = leveraged ETF (5% alloc) · Bucket B = swing trade · "
        f"Stop {sm}×ATR · Target {tm}×ATR · R:R 1:{round(tm/sm,1)} · "
        f"Min score {mn}/{MAX} · "
        f"Sorted: signals first, then by score · Click row to expand"
    )

    # Fetch live VIX
    vix_val             = fetch_vix()
    vix_lbl, vix_col, vix_sub = vix_regime(vix_val)

    # Fetch yfinance news → logs/market_news.json (background, non-blocking)
    try:
        _fetch_yfinance_news()
    except Exception as _yfe:
        logger.warning(f"yfinance news fetch failed — news data absent this cycle: {_yfe}")

    results_by_sym   = {r["symbol"]: r for r in results}
    # Filter pinned symbols (SPY/QQQ) from active rows — they display in the pinned
    # scan rows above with an inline "IN POSITION" badge instead of appearing twice.
    _pinned_set = set(PINNED)
    open_trades_for_active = {k: v for k, v in open_trades.items() if k not in _pinned_set}
    active_rows_html = build_active_rows(open_trades_for_active, results_by_sym)
    # ── Tier the watchlist by conviction (redesign 2026-07-05) ────────────────
    def _score_of(r):
        return max(r.get("long_score", 0), r.get("short_score", 0))
    _high  = [r for r in sorted_rest if _score_of(r) >= _full]
    _watch = [r for r in sorted_rest if _half <= _score_of(r) < _full]
    _below = [r for r in sorted_rest if _score_of(r) < _half]

    def _tier_div(label, color, n):
        tail = (f'<span style="color:#5a6580;font-weight:400"> &middot; {n}</span>' if n
                else '<span style="color:#5a6580;font-weight:400"> &mdash; none</span>')
        return (f'<tr><td colspan="9" style="padding:8px 14px;font-size:11px;'
                f'font-weight:700;letter-spacing:.08em;text-transform:uppercase;'
                f'color:{color};background:#0a0d14;border-top:1px solid #252847">'
                f'{label}{tail}</td></tr>')

    _b = len(pinned)
    rows_html = (
        build_rows(pinned, open_now, idx_offset=0, pm_extra=_pm_extra, pm_all=_pm_all, confirm_gate=confirm_gate, open_trades=open_trades)
        + active_rows_html
        + _tier_div(f"&#9650; Highest Conviction &middot; score &ge; {_full}", "#30d158", len(_high))
        + build_rows(_high, open_now, idx_offset=_b, pm_extra=_pm_extra, pm_all=_pm_all, confirm_gate=confirm_gate)
        + _tier_div(f"&#9670; Watchlist &middot; score {_half}&ndash;{_full - 1}", "#ffd60a", len(_watch))
        + build_rows(_watch, open_now, idx_offset=_b + 300, pm_extra=_pm_extra, pm_all=_pm_all, confirm_gate=confirm_gate)
        + _tier_div(f"Below threshold &middot; score &lt; {_half}", "#8a94ae", len(_below))
        + build_rows(_below, open_now, idx_offset=_b + 600, pm_extra=_pm_extra, pm_all=_pm_all, confirm_gate=confirm_gate)
    )

    # Composite regime BAR removed (redesign 2026-07-05) — it lives on the
    # dashboard. cr is still fetched below: the 0DTE rec logic consumes it.
    cr = _get_composite_regime_display()
    composite_bar_html = ""

    # ── Implied range + 0DTE rec ─────────────────────────────────────────────
    spy_0dte  = _fetch_spy_0dte_data()
    _dte_prev = _load_dte_prev()           # capture before _compute saves new rec
    dte_rec   = _compute_0dte_rec(results_by_sym.get("SPY"), spy_0dte, vix_val, cr)

    # ── 0DTE alert: detect direction flip or large strike move ───────────────
    # TB-5: Phantom flip guard — a direction change that has only persisted for
    # 1 scan is marked "UNCONFIRMED" (phantom flip).  Alerts only show as
    # CONFIRMED after the same direction appears on 2+ consecutive scans.
    _dte_alert = None
    _prev_confirm_count = _dte_prev.get("confirm_count", 1) if _dte_prev else 1
    if not dte_rec.get("no_rec"):
        _curr_dir    = dte_rec["direction"]
        _curr_strike = dte_rec["strike"]
        _prev_dir    = _dte_prev.get("direction") if _dte_prev else None
        _prev_strike = _dte_prev.get("strike")    if _dte_prev else None
        _dir_changed = _prev_dir and _prev_dir != _curr_dir
        # Track consecutive scan count for current direction (phantom flip guard)
        _new_confirm = 1 if _dir_changed else (_prev_confirm_count + 1 if _dte_prev else 1)
        # Save updated confirm count back to state file
        try:
            _save_dte_prev(dte_rec, confirm_count=_new_confirm)
        except Exception as _sdp_e:
            logger.debug("write_html: _save_dte_prev failed — %s", _sdp_e)
        _strike_moved = (_prev_strike and _curr_strike and
                         abs(_curr_strike - _prev_strike) >= 5)   # AB-audit: raised from $3 — SPY naturally moves 1-2 strikes intraday
        if (_dte_prev and (_dir_changed or _strike_moved)):
            _prev_ts  = _dte_prev.get("ts", "")
            try:
                _prev_pt = (datetime.fromisoformat(_prev_ts)
                            .astimezone(_PST).strftime("%-I:%M %p PT"))
            except Exception as _dtepts_err:
                logger.debug("[scan] DTE prev timestamp PT format failed — using '—': %s", _dtepts_err)
                _prev_pt = "—"
            _change_desc = (f"direction {_prev_dir}→{_curr_dir}"
                            if _dir_changed
                            else f"strike moved ${_prev_strike:.0f}→${_curr_strike:.0f}")
            # TB-5: flag as phantom if new direction has only 1 confirming scan
            _is_phantom = _dir_changed and _new_confirm < 2
            _dte_alert = {
                "direction":   _curr_dir,
                "strike":      _curr_strike,
                "size":        dte_rec["size"],
                "change":      _change_desc,
                "prev_ts":     _prev_pt,
                "phantom":     _is_phantom,
                "confirm":     _new_confirm,
            }
    # Implied-range bar REMOVED (redesign 2026-07-05) — noise on scan; the 0DTE
    # tile carries the actionable directional read.
    implied_bar_html = ""

    # PDT status bar removed — SEC/FINRA rule amendment, board vote S50 28-0.

    # ── TB-4: Bot health header ───────────────────────────────────────────────
    # Katsuyama/Majors: at-a-glance bot status — SPY event, session P&L vs kill
    # switch, TQI rolling avg, last scan timestamp.  Reads from data dict
    # populated by write_scan_html(); gracefully absent in standalone mode.
    _bh_spy_event  = data.get("spy_event_type") or ""
    # Prefer Alpaca-authoritative daily P&L (equity−last_equity, written by
    # generate_dashboard.py) over risk.daily_pnl which resets each bot restart.
    _bh_daily_pnl = data.get("daily_pnl")
    try:
        import pathlib as _pl
        import json as _json
        _pnl_data = _json.loads(
            (_pl.Path(__file__).parent / "logs" / "daily_pnl_cache.json").read_text()
        )
        _cache_pnl = _pnl_data.get("daily_pnl")
        if _cache_pnl is not None:
            _bh_daily_pnl = _cache_pnl
    except Exception as _cr_e:
        logger.debug(
            "write_html: daily_pnl_cache read failed — stale cache, no output impact: %s",
            _cr_e,
        )
    # Bot-health bar REMOVED (redesign 2026-07-05) — P&L / TQI / SPY-event belong
    # on the dashboard, not as per-scan noise.
    bot_health_bar_html = ""

    # ── 0DTE tile HTML + alert banner (pre-computed to keep f-string clean) ────
    # TB-5: Phantom flip guard — if direction just flipped and has < 2 confirming
    # scans, mark alert as UNCONFIRMED with an orange "PHANTOM?" badge so the
    # operator knows to wait for a second scan before acting.
    if _dte_alert:
        _al       = _dte_alert
        _al_col   = "#30d158" if _al["direction"] == "CALL" else "#ff3b30"
        _al_label = "⚡ NEW CALL ALERT" if _al["direction"] == "CALL" else "⚡ NEW PUT ALERT"
        # TB-5: phantom badge
        _phantom_badge = (
            '<span style="font-size:10px;font-weight:700;padding:2px 7px;border-radius:3px;'
            'background:rgba(255,159,10,.2);color:#ff9f0a;margin-left:6px">'
            '⚠ UNCONFIRMED · WAIT 1 MORE SCAN</span>'
            if _al.get("phantom") else
            f'<span style="font-size:10px;color:#30d158;margin-left:6px">'
            f'✓ {_al.get("confirm", 2)} scans confirmed</span>'
        )
        dte_alert_html = (
            f'<div id="dte-alert-banner" style="background:rgba(255,214,10,.08);'
            f'border:1px solid rgba(255,214,10,.3);border-radius:6px;'
            f'padding:10px 18px;margin:10px 20px 0;display:flex;align-items:center;gap:14px">'
            f'<span style="font-size:13px;font-weight:800;color:#ffd60a;letter-spacing:.06em">'
            f'{_al_label}</span>'
            f'<span style="font-size:13px;font-weight:700;color:{_al_col}">'
            f'${_al["strike"]:.0f} {_al["direction"]} · Size {_al["size"]}</span>'
            f'{_phantom_badge}'
            f'<span style="font-size:11px;color:#b8bdd4">'
            f'{_al["change"]} · was {_al["prev_ts"]}</span>'
            f'<span style="font-size:10px;color:#4a5070;margin-left:auto;cursor:pointer" '
            f'onclick="document.getElementById(\'dte-alert-banner\').style.display=\'none\'">'
            f'✕ dismiss</span>'
            f'</div>'
        )
    else:
        dte_alert_html = ""

    if not dte_rec.get("no_rec"):
        _d         = dte_rec
        _dir_col   = "#30d158" if _d["direction"] == "CALL" else "#ff3b30"
        _mv        = f'±{_d["move_pct"]:.1f}%' if _d["move_pct"] is not None else "—"
        _delta_str = f'Δ {_d["delta"]:.3f}' if _d.get("delta") is not None else ""
        _otm_str   = f'{_d["otm_pct"]:.2f}% OTM' if _d.get("otm_pct") is not None else ""
        _mid_str   = f'${_d["mid"]:.2f}' if _d.get("mid") is not None else "—"
        _bias_col  = ("#30d158" if _d["bias"] in ("bull", "strong_bull")
                      else "#ff3b30" if _d["bias"] in ("bear", "strong_bear")
                      else "#ffd60a")
        _max_score  = sum(config.SCORE_WEIGHTS.values())
        _spy_score  = _d.get("spy_score") or 0
        _is_perfect = _spy_score >= _max_score
        _size_signal_map = {
            ("CALL", "3"): ("▲▲ STRONG BULL", "#30d158") if _is_perfect else ("▲ BULL", "#30d158"),
            ("CALL", "2"): ("▲ BULL",         "#30d158"),
            ("CALL", "1"): ("▲ BULL",         "#30d158"),
            ("PUT",  "3"): ("▼▼ STRONG BEAR",  "#ff3b30") if _is_perfect else ("▼ BEAR", "#ff3b30"),
            ("PUT",  "2"): ("▼ BEAR",          "#ff3b30"),
            ("PUT",  "1"): ("▼ BEAR",          "#ff3b30"),
        }
        _size_lbl, _size_col = _size_signal_map.get(
            (_d["direction"], _d["size"]), ("—", "#5a6080")
        )
        # Size tier label for confidence display
        _conf_map  = {"3": ("FULL",    "#30d158"), "2": ("MED",     "#ffd60a"), "1": ("REDUCED", "#ff9f0a")}
        _conf_lbl, _conf_col = _conf_map.get(str(_d["size"]), ("—", "#b8bdd4"))
        # Bias note: only shown if bias conflicts with direction (VWAP/stickiness override)
        _call_bias = _d["bias"] in ("bull", "strong_bull")
        _put_bias  = _d["bias"] in ("bear", "strong_bear")
        _conflict  = (_d["direction"] == "CALL" and _put_bias) or (_d["direction"] == "PUT" and _call_bias)
        _bias_note = (
            f'<span style="font-size:10px;color:#ff9f0a;margin-left:8px">'
            f'⚡ VWAP override · raw bias: {_d["bias_display"]}</span>'
        ) if _conflict else ""

        _contracts_str = _d.get("contracts_str", f'{_d["size"]} CONTRACTS')
        _size_reason   = _d.get("size_reason", "")
        _vix_display   = f'VIX {_d["vix_val"]:.1f}' if _d.get("vix_val") else ""
        _nearest_lbl   = _d.get("nearest_level")

        # Size action label: "BUY 2 CONTRACTS — VIX elevated"
        _size_action = f'BUY {_contracts_str}'
        _size_sub    = _size_reason if _size_reason else _vix_display

        # Sigma context lines
        _s1l = _d.get("sigma_1_low")
        _s1h = _d.get("sigma_1_high")
        _s2l = _d.get("sigma_2_low")
        _s2h = _d.get("sigma_2_high")
        _sigma_line = ""
        if _s1l and _s1h:
            _sigma_line = (
                f'1σ range <b style="color:#e2e4ee">${_s1l:.2f} – ${_s1h:.2f}</b>'
                f' &nbsp;·&nbsp; 2σ <b style="color:#e2e4ee">${_s2l:.2f} – ${_s2h:.2f}</b>'
                if _s2l and _s2h else
                f'1σ range <b style="color:#e2e4ee">${_s1l:.2f} – ${_s1h:.2f}</b>'
            )

        # Fib levels table (compact)
        _fibs = _d.get("fib_levels", {})
        _fib_lines = ""
        if _fibs:
            _price_now = spy_0dte.get("price", 0) if spy_0dte else 0
            _fl = [
                ("78.6%", _fibs.get("fib_786")),
                ("61.8%", _fibs.get("fib_618")),
                ("50.0%", _fibs.get("fib_500")),
                ("38.2%", _fibs.get("fib_382")),
                ("23.6%", _fibs.get("fib_236")),
            ]
            _fib_parts = []
            for _fn, _fv in _fl:
                if _fv is not None:
                    _fc = "#30d158" if _fv < _price_now else "#ff3b30"
                    _fib_parts.append(f'<span style="color:#b8bdd4">{_fn}</span> <b style="color:{_fc}">${_fv:.2f}</b>')
            if _fib_parts:
                _fib_lines = " &nbsp;·&nbsp; ".join(_fib_parts)

        # Prior day S/R
        _pdh = _fibs.get("prior_high")
        _pdl = _fibs.get("prior_low")
        _sr_line = ""
        if _pdh and _pdl:
            _sr_line = (f'Prev day: high <b style="color:#ff3b30">${_pdh:.2f}</b>'
                        f' &nbsp;·&nbsp; low <b style="color:#30d158">${_pdl:.2f}</b>')

        # ── Exec Director synthesis ───────────────────────────────────────────
        _exec_target = _s1l if _d["direction"] == "PUT" else _s1h
        _exec_stop   = (_fibs.get("prior_high") if _d["direction"] == "PUT"
                        else _fibs.get("prior_low"))
        _exec_conf   = f' · {_nearest_lbl}' if _nearest_lbl else ""
        _exec_theta_warn = ""
        if _d.get("hours_rem") is not None:
            _hrs = _d["hours_rem"]
            if _hrs < 1.5:
                _exec_theta_warn = f' · <span style="color:#ff3b30;font-weight:700">THETA CRITICAL — {_hrs:.1f}h left, reduce to 1</span>'
            elif _hrs < 2.5:
                _exec_theta_warn = f' · <span style="color:#ff9f0a">⚠ {_hrs:.1f}h to expiry — consider scaling out</span>'

        # ── T1/T2 tranche targets (AB-audit: Sosnoff/Sinclair/Nathan) ─────────
        # T1 (SPY): 50% of move from current price to 1σ — scale out half here
        # T2 (SPY): full 1σ level — let the runner ride
        # T1 (option): 2× entry mid (100% gain) — standard tastyworks first target
        # T2 (option): 3× entry mid (200% gain) — runner target or theta exit
        _spy_now    = spy_0dte.get("price", 0) if spy_0dte else 0
        _t2_spy     = _exec_target
        _t1_spy     = (round(_spy_now + (_t2_spy - _spy_now) * 0.50, 2)
                       if _t2_spy and _spy_now > 0 else None)
        _mid_val    = _d.get("mid")
        _t1_opt     = round(_mid_val * 2.0, 2) if _mid_val and _mid_val > 0 else None
        _t2_opt     = round(_mid_val * 3.0, 2) if _mid_val and _mid_val > 0 else None
        _t1_spy_str = f'${_t1_spy:.2f}' if _t1_spy else "½ × 1σ"
        _t2_spy_str = f'${_t2_spy:.2f}' if _t2_spy else "1σ level"
        _t1_opt_str = f' <span style="color:#b8bdd4;font-size:11px">(opt ${_t1_opt:.2f})</span>' if _t1_opt else ""
        _t2_opt_str = f' <span style="color:#b8bdd4;font-size:11px">(opt ${_t2_opt:.2f})</span>' if _t2_opt else ""

        _exec_stop_str   = f'${_exec_stop:.2f}' if _exec_stop else 'prior day extreme'
        _spy_score_part  = (f'{_d["spy_score"]}/{sum(config.SCORE_WEIGHTS.values())} indicators'
                            if _d.get("spy_score") else "—")
        _exec_html = (
            f'<div style="margin-top:10px;padding:10px 14px;background:rgba(0,229,255,0.05);'
            f'border:1px solid rgba(0,229,255,0.2);border-radius:4px">'
            f'<div style="font-size:9px;font-weight:700;color:#00e5ff;text-transform:uppercase;'
            'letter-spacing:.12em;margin-bottom:6px">&#9654; 0DTE Exec Director</div>'
            '<div style="font-size:12px;color:#e2e4ee;line-height:1.9">'
            f'<b style="color:{_dir_col}">BUY {_contracts_str} × ${_d["strike"]:.0f} {_d["direction"]}</b>'
            f'{_exec_conf}<br>'
            f'<span style="color:#ffd60a;font-weight:700">T1</span> SPY <b style="color:#30d158">{_t1_spy_str}</b>{_t1_opt_str}'
            f' &nbsp;·&nbsp; <span style="color:#ffd60a;font-weight:700">T2</span> SPY <b style="color:#30d158">{_t2_spy_str}</b>{_t2_opt_str}<br>'
            f'Stop: thesis broken if SPY reclaims <b style="color:#ff3b30">{_exec_stop_str}</b><br>'
            f'Basis: {_d["bias_display"]} bias · {_spy_score_part} · VWAP-confirmed{_exec_theta_warn}'
            '</div>'
            '</div>'
        )

        dte_tile_html = (
            '<div style="cursor:pointer;user-select:none" onclick="togDTE()">'
            + f'<div style="font-size:20px;font-weight:700;color:{_dir_col};margin-top:2px">'
            + f'${_d["strike"]:.0f} {_d["direction"]}'
            + (f'<span style="font-size:11px;color:#b8bdd4;margin-left:8px">{_delta_str}</span>' if _delta_str else '')
            + '<span id="dte-arr" style="font-size:10px;color:#b8bdd4;margin-left:6px">▼</span>'
            + '</div>'
            + '<div style="margin-top:3px">'
            + f'<span style="font-size:12px;font-weight:700;color:{_size_col};letter-spacing:.06em">{_size_lbl}</span>'
            + '<span style="font-size:11px;color:#b8bdd4;margin-left:10px">'
            + f'<span style="color:{_conf_col};font-weight:700">{_size_action}</span>'
            + (f' <span style="color:#b8bdd4">— {_size_sub}</span>' if _size_sub else '')
            + '</span>'
            + f'{_bias_note}'
            + '</div>'
            + '</div>'
            + '<div id="dte-detail" style="display:none;margin-top:8px;'
            + 'border-top:1px solid #1e2440;padding-top:8px">'
            + '<div style="font-size:11px;color:#b8bdd4;line-height:2">'
            + f'IV <b style="color:#e2e4ee">{_d["iv"]:.1f}%</b>'
            + f' &nbsp;·&nbsp; OI <b style="color:#e2e4ee">{_d["oi"]:,}</b>'
            + (f' &nbsp;·&nbsp; OTM <b style="color:#e2e4ee">{_otm_str}</b>' if _otm_str else '')
            + f' &nbsp;·&nbsp; Mid <b style="color:#e2e4ee">{_mid_str}</b><br>'
            + (f'{_sigma_line}<br>' if _sigma_line else '')
            + (f'{_sr_line}<br>' if _sr_line else '')
            + f'P/C OI <b style="color:#e2e4ee">{_d["pc_label"]}</b>'
            + f' &nbsp;·&nbsp; IV Skew <b style="color:#e2e4ee">{_d.get("skew_label", "—")}</b><br>'
            + f'Move <b style="color:#e2e4ee">{_mv}</b>'
            + f' &nbsp;·&nbsp; Exp <b style="color:#e2e4ee">{_d["expiry"]}</b><br>'
            + (f'Fib levels: {_fib_lines}<br>' if _fib_lines else '')
            + (f'<span style="color:#ffd60a">⚡ Near: {_nearest_lbl}</span>' if _nearest_lbl else '')
            + '</div>'
            + f'{_exec_html}'
            + '</div>'
        )
    else:
        _no_rec_reason = dte_rec.get("no_rec_reason", "No setup · neutral regime")
        # Fall back to last known recommendation from dte_prev.json so the tile
        # is never empty — shows cached direction/strike with a "LAST KNOWN" label.
        _prev_dir    = _dte_prev.get("direction")    if _dte_prev else None
        _prev_strike = _dte_prev.get("strike")       if _dte_prev else None
        _prev_size   = _dte_prev.get("size",   "?")  if _dte_prev else "?"
        _prev_ts     = (_dte_prev.get("ts", "")[:10] if _dte_prev and _dte_prev.get("ts") else "")
        _prev_conf   = _dte_prev.get("confirm_count", 1) if _dte_prev else 1
        if _prev_dir and _prev_strike:
            _pdir_col = "#30d158" if _prev_dir == "CALL" else "#ff3b30"
            dte_tile_html = (
                f'<div style="font-size:18px;font-weight:700;color:{_pdir_col};margin-top:2px">'
                f'${_prev_strike:.0f} {_prev_dir}</div>'
                f'<div style="margin-top:3px">'
                f'<span style="font-size:10px;font-weight:700;padding:1px 6px;border-radius:3px;'
                f'background:rgba(255,214,10,.12);color:#ffd60a;letter-spacing:.04em">LAST KNOWN</span>'
                f'<span style="font-size:10px;color:#b8bdd4;margin-left:6px">{_no_rec_reason}</span>'
                f'</div>'
                f'<div style="font-size:10px;color:#4a5070;margin-top:2px">'
                f'Size {_prev_size} · {_prev_ts} · {_prev_conf} scans'
                f'</div>'
            )
        else:
            dte_tile_html = (
                '<div style="font-size:20px;font-weight:700;color:#b8bdd4;margin-top:2px">—</div>'
                f'<div style="font-size:11px;color:#b8bdd4;margin-top:2px">{_no_rec_reason}</div>'
            )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="{refresh}">
<script>setTimeout(function(){{location.reload(true);}},{refresh}000);</script>
<title>MTF Scanner · {pt_time}</title>
<style>{CSS}</style>
</head>
<body>

<div style="display:flex;align-items:center;justify-content:space-between;padding:13px 20px;
  background:#161920;border-bottom:1px solid #161a28;position:sticky;top:0;z-index:10">
  <div style="display:flex;align-items:center;gap:16px">
    <div>
      <span style="font-size:15px;font-weight:700;letter-spacing:-.01em">Raf's Signal Scanner</span>
      <span style="font-size:11px;color:#b8bdd4;margin-left:10px">{len(results)} tickers{pv_str} · ranked by score · {pt_time} &nbsp;·&nbsp; {LIVE_CLOCK_HTML}</span>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:16px">
    <a href="options.html" style="font-size:11px;font-weight:600;color:#b8bdd4;text-decoration:none;
      padding:5px 11px;border:1px solid #2a2f48;border-radius:6px;white-space:nowrap;
      transition:color .15s,border-color .15s" onmouseover="this.style.color='#e2e4ee';this.style.borderColor='#4a5070'"
      onmouseout="this.style.color='#b8bdd4';this.style.borderColor='#2a2f48'">Options ↗</a>
    <div class="scan-pill {pill_class}">
      <div class="scan-pulse"></div>
      <span>{pill_text}</span>
    </div>
    <span id="scan-countdown" style="font-size:11px;color:#b8bdd4">Next scan …</span>
  </div>
</div>

<div style="display:flex;gap:1px;background:#161a28;border-bottom:1px solid #161a28">
  <div style="flex:1;padding:12px 18px;background:#161920">
    <div class="label">TRADE signals</div>
    <div style="font-size:26px;font-weight:700;color:{"#30d158" if trades else "#4a5070"}">{len(trades)}</div>
    <div style="font-size:11px;color:#b8bdd4;margin-top:2px">{", ".join(trades) if trades else trade_empty_lbl}</div>
  </div>
  <div style="flex:1;padding:12px 18px;background:#161920">
    <div class="label">WATCH signals</div>
    <div style="font-size:26px;font-weight:700;color:#ffd60a">{len(longs)+len(shorts)-len(trades)}</div>
    <div style="font-size:11px;color:#b8bdd4;margin-top:2px">↑{len(longs)} long · ↓{len(shorts)} short · below threshold</div>
  </div>
  <div style="flex:1;padding:12px 18px;background:#161920">
    <div class="label">Watchlist Regime</div>
    <div style="font-size:20px;font-weight:700;color:{regime_col};margin-top:2px">{regime_label}</div>
    <div style="font-size:11px;color:#b8bdd4;margin-top:2px">{regime_sub}</div>
  </div>
  <div style="flex:1;padding:12px 18px;background:#161920">
    <div class="label">Variable Watchlist</div>
    <div style="font-size:26px;font-weight:700;color:{"#ffd60a" if _pm_extra else "#4a5070"}">{len(_pm_extra)}</div>
    <div style="font-size:11px;color:#b8bdd4;margin-top:2px">{"PM movers: " + ", ".join(sorted(_pm_extra)) if _pm_extra else "no pre-market additions today"}</div>
  </div>
  <div style="flex:1.4;padding:12px 18px;background:#161920;border-left:1px solid #1e2440">
    <div class="label" style="margin-bottom:4px">0DTE SPY</div>
    {dte_tile_html}
  </div>
</div>
{composite_bar_html}{implied_bar_html}
{bot_health_bar_html}
{dte_alert_html}
<table>
  <thead>
    <tr>
      <th>Ticker</th>
      <th>Price</th>
      <th>Bias</th>
      <th>Score</th>
      <th title="Consecutive scans confirming the same signal direction — ✓ 2/2 = confirmed on 2 consecutive scans. Bot requires 2 qualifying scans before entry (phantom flip guard).">Confirm ⓘ</th>
      <th>Signal</th>
      <th>Rel Vol</th>
      <th>Momentum</th>
      <th></th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>

<div style="padding:10px 20px;font-size:10px;color:#b8bdd4;border-top:1px solid #161a28">
  {footer_legend}
</div>

<script>
function togDTE(){{
  var d=document.getElementById("dte-detail");
  var a=document.getElementById("dte-arr");
  if(!d) return;
  if(d.style.display==="none"||d.style.display===""){{
    d.style.display="block";if(a)a.innerHTML="▲";
  }}else{{d.style.display="none";if(a)a.innerHTML="▼";}}
}}
function tog(i){{
  var d=document.getElementById("det-"+i);
  var a=document.getElementById("arr-"+i);
  if(d.style.display==="none"||d.style.display===""){{
    d.style.display="table-row";a.innerHTML="▲";
  }}else{{d.style.display="none";a.innerHTML="▼";}}
}}

// ── Next-scan countdown ───────────────────────────────────────────────────────
(function(){{
  // Last scan timestamp (ET, written by Python)
  var lastScanISO = "{scan_time}";
  // Scan intervals by session (seconds)
  var PREMARKET_SECS = 10 * 60;   // 8:45–9:30 ET
  var RTH_SECS       = 5  * 60;   // 9:30–16:00 ET
  var AH_SECS        = 30 * 60;   // all other times

  function etMinutes(d) {{
    // Convert a Date to minutes-since-midnight in ET
    var etStr = d.toLocaleString("en-US", {{timeZone:"America/New_York", hour12:false, hour:"2-digit", minute:"2-digit"}});
    var parts = etStr.split(":");
    return parseInt(parts[0],10)*60 + parseInt(parts[1],10);
  }}

  function sessionInterval(d) {{
    var m = etMinutes(d);
    var wday = new Date(d.toLocaleString("en-US",{{timeZone:"America/New_York"}})).getDay();
    if(wday===0||wday===6) return AH_SECS; // weekend
    if(m >= 8*60+45 && m < 9*60+30) return PREMARKET_SECS;
    if(m >= 9*60+30 && m < 16*60)   return RTH_SECS;
    return AH_SECS;
  }}

  function sessionLabel(d) {{
    var m = etMinutes(d);
    var wday = new Date(d.toLocaleString("en-US",{{timeZone:"America/New_York"}})).getDay();
    if(wday===0||wday===6) return "overnight";
    if(m >= 8*60+45 && m < 9*60+30) return "pre-mkt 10m";
    if(m >= 9*60+30 && m < 16*60)   return "RTH 5m";
    return "overnight";
  }}

  function fmtCountdown(secs) {{
    if(secs <= 0) return "scanning now…";
    var m = Math.floor(secs/60), s = secs%60;
    return m + ":" + (s<10?"0":"") + s;
  }}

  var lastScan = new Date(lastScanISO.replace(" ","T"));
  // If parse failed, fall back to now
  if(isNaN(lastScan.getTime())) lastScan = new Date();
  // Treat naive ISO as ET by applying offset
  var rawStr = lastScanISO.trim();
  if(!rawStr.match(/[Zz]|[+-]\\d{{2}}:\\d{{2}}$/)) {{
    // naive → treat as ET; compute ET offset at that moment
    var etOffset = new Date(rawStr + "Z").getTime() - new Date(new Date(rawStr + "Z").toLocaleString("en-US",{{timeZone:"America/New_York"}})).getTime();
    lastScan = new Date(new Date(rawStr + "Z").getTime() - etOffset);
  }}

  var el = document.getElementById("scan-countdown");
  if(!el) return;

  function tick() {{
    var now   = new Date();
    var interval = sessionInterval(lastScan);
    var nextScan = new Date(lastScan.getTime() + interval * 1000);
    var secsLeft = Math.round((nextScan - now) / 1000);
    var label = sessionLabel(lastScan);
    var refreshedStr = lastScan.toLocaleTimeString("en-US",{{timeZone:"America/Los_Angeles",hour:"numeric",minute:"2-digit",hour12:true,timeZoneName:"short"}}).replace(":00 "," ");
    if(secsLeft > 0) {{
      el.innerHTML = "Next scan in <b style='color:#e2e4ee'>" + fmtCountdown(secsLeft) + "</b> <span style='color:#b8bdd4'>(" + label + " · last <b style='color:#e2e4ee'>" + refreshedStr + "</b>)</span>";
    }} else {{
      el.innerHTML = "<b style='color:#ffd60a'>Scanning now…</b> <span style='color:#b8bdd4'>(last <b style='color:#e2e4ee'>" + refreshedStr + "</b>)</span>";
    }}
  }}

  tick();
  setInterval(tick, 1000);
}})();
</script>
</body>
</html>"""

    tmp = OUT_HTML + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(tmp, OUT_HTML)
        print(f"  ✓ Written → {OUT_HTML}", flush=True)
    except Exception as e:
        logger.warning(f"Standalone HTML write failed: {e}", exc_info=True)
        print(f"  ✗ HTML write failed: {e}", flush=True)
        try:
            os.remove(tmp)
        except Exception as _rm_e:
            logger.debug("write_html: tmp file cleanup failed — %s", _rm_e)
    return OUT_HTML


def write_scan_html(signals: "list | None" = None, portfolio_value: "float | None" = None,
                    open_trades: "dict | None" = None,
                    spy_event_type: str = "",
                    daily_pnl: "float | None" = None,
                    tqi_history: "list | None" = None,
                    confirm_gate: "dict | None" = None):
    """
    Standalone callable for main.py bot loop.
    Runs a fresh scan and writes scan_results.html atomically.
    Never opens a browser. Safe to call after every run_cycle().
    signals:         optional pre-computed signals list (unused currently, reserved)
    portfolio_value: current portfolio value for display
    open_trades:     tracker.open_trades dict for pinned active-position rows; None = skip section
    spy_event_type:  TB-4 bot health — current SPY hybrid event type (EXTREME, BROAD_*, etc.)
    daily_pnl:       TB-4 bot health — session P&L for kill switch proximity display
    tqi_history:     TB-4 bot health — rolling TQI list for quality score display
    """
    try:
        tickers = list(config.WATCHLIST)
        for sym in ["SPY", "QQQ"]:
            if sym not in tickers:
                tickers.append(sym)

        # ── Variable watchlist: inject today's pre-market movers ──────────────
        # Populated by main.py pre-market phase → logs/premarket_movers.json
        _pm_extra: list = []   # tickers added beyond core watchlist (for count display)
        _pm_all:   set  = set()  # ALL pre-market movers (for dotted border + badge)
        try:
            import json as _j
            _pm_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "logs", "premarket_movers.json")
            with open(_pm_path) as _pf:
                _pm_data = _j.load(_pf)
            _pm_ts = datetime.fromisoformat(_pm_data.get("ts", "2000-01-01T00:00:00"))
            if _pm_ts.tzinfo is None:
                _pm_ts = _pm_ts.replace(tzinfo=ET)
            _pm_today = _pm_ts.astimezone(ET).date() == datetime.now(ET).date()
            if _pm_today:
                _core = set(_pm_data.get("core_watchlist", config.WATCHLIST))
                for _sym in _pm_data.get("movers", []):
                    _pm_all.add(_sym)               # all movers get visual treatment
                    if _sym not in tickers:
                        tickers.append(_sym)
                        if _sym not in _core:
                            _pm_extra.append(_sym)  # truly new additions to scan universe
        except Exception as _pm_err:
            # no movers file yet — standalone mode or pre-market hasn't run
            logger.debug("[scan] PM movers file load failed — standalone or file not ready: %s", _pm_err)

        # U-1: _scan_ticker_with_timeout now uses threading.Thread + join(timeout)
        # instead of signal.SIGALRM. Safe to call from any thread — no main-thread
        # restriction. ThreadPoolExecutor is still avoided to keep scan sequential
        # and prevent yfinance rate-limit saturation under concurrent fetches.
        data = run_scan(tickers)
        data["portfolio_value"]  = portfolio_value
        data["open_trades"]      = open_trades       # None = standalone → active section hidden
        data["pm_extra"]         = _pm_extra         # off-watchlist additions (count display)
        data["pm_all"]           = list(_pm_all)     # all PM movers (row visual treatment)
        data["spy_event_type"]   = spy_event_type    # TB-4 bot health header
        data["daily_pnl"]        = daily_pnl         # TB-4 bot health header
        data["tqi_history"]      = tqi_history or [] # TB-4 bot health header
        data["confirm_gate"]     = confirm_gate or {} # BoD-1 confirm gate: primed tickers
        write_html(data)
    except Exception as e:
        logger.warning(f"write_scan_html failed: {e}", exc_info=True)
        print(f"  ✗ write_scan_html failed: {e}", flush=True)


# _load_pdt_standalone() deleted S52 — PDT removed per SEC/FINRA rule amendment, board vote S50 28-0.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch",   action="store_true")
    parser.add_argument("--tickers", nargs="+", default=config.WATCHLIST)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    print("="*60)
    print("  MTF scan_to_html.py")
    print(f"  Tickers: {len(args.tickers)} ({args.tickers})")
    print(f"  Output:  {OUT_HTML}")
    print("="*60)

    # Always include SPY, QQQ as market context rows
    for sym in ["SPY", "QQQ"]:
        if sym not in args.tickers:
            args.tickers.append(sym)

    # First scan + write
    data = run_scan(args.tickers)
    html_path = write_html(data)

    # Browser open disabled — open scan_results.html manually once in Chrome
    # Chrome will auto-reload via meta refresh tag every scan cycle

    if not args.watch:
        print(f"  Done. File: {html_path}")
        print("  Chrome will auto-reload the tab — do not run this script again to refresh.")
        return

    # Watch loop — rewrites HTML file in place, Chrome auto-reloads via meta refresh
    # Never opens a new window here
    while True:
        try:
            on = bool(is_market_open())
        except Exception as _mktopen2_e:
            logger.warning("watch loop: is_market_open() failed — falling back to time-based check: %s", _mktopen2_e)
            on = _market_open_by_time()
        iv    = 300 if on else 1800   # 5min open, 30min closed
        label = "5min (market open)" if on else "30min (market closed)"
        print(f"\n  Next scan in {label}...", flush=True)
        time.sleep(iv)
        data = run_scan(args.tickers)
        write_html(data)
        print("  HTML updated in place — Chrome reloads automatically.", flush=True)


if __name__ == "__main__":
    try:    main()
    except KeyboardInterrupt: print("\n\nStopped.")
