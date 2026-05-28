# ruff: noqa: E501, E701
"""
strategy/signal_generator.py
Orchestrates the full scan: fetches data, scores every ticker, returns ranked signals.

Two scoring systems run in parallel every cycle:
  - 12-point system: drives all entry/exit decisions (unchanged)
  - 16-point system: log only, for validation against 12pt over time
"""

import glob
import json
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _FutTimeout
from datetime import datetime, date as _date, timedelta as _td
from pathlib import Path
from zoneinfo import ZoneInfo

from data.fetcher import get_universe, fetch_multi_timeframe, fetch_bars
from data.fmp_client import get_earnings_surprise
from data.premarket import build_scan_universe
from events.calendar import is_macro_event_day
from strategy.confluence import prepare_df, score_long_signal, score_short_signal
from indicators.macd import macd_bearish_cross, macd_bullish_cross
from indicators.moving_averages import ema_bearish_cross, ema_bullish_cross
from indicators.momentum import get_momentum_summary
import config

# Absolute path anchor — prevents CWD-relative write failures (same pattern as kelly.py TB-2 fix)
_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

# ── NEW-2 (PEAD): per-symbol earnings surprise cache ─────────────────────────
# Keyed by symbol. Refreshed once per calendar day (daily_reset when date changes).
_pead_cache: dict = {}        # {symbol: {"beat_pct": float, "report_date": date} | None}
_pead_cache_date: str = ""    # ET date string of last cache population

# ── NEW-3: sector map for residual momentum (board-approved 2026-04-20) ───────
from data.sectors import SECTOR_MAP_SG as _SECTOR_MAP_SG  # noqa: E402

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# ── 16-point system thresholds (log only — not traded) ───────────────────────
SCORE_16PT_MAX     = 20   # extended: +c10 IBS, +c11 Pivot S/R, +c12 FOMC, +c13 PEAD (board-approved 2026-04-20)
SCORE_16PT_MIN     = 11   # minimum signal equivalent
SCORE_16PT_HALF    = 13   # half size threshold
SCORE_16PT_FULL    = 14   # full size threshold
SCORE_16PT_PDT_MIN = 13   # PDT=3 swing mode minimum
SCORE_16PT_REVERSAL_ENTRY_DROP  = 7   # entry direction drops below this
SCORE_16PT_REVERSAL_OPPOSITE    = 10  # opposite direction rises above this

# ── Weekly bias cache (60-minute TTL) ────────────────────────────────────────
_weekly_bias_cache: dict = {}
_WEEKLY_BIAS_REFRESH_SECS = 3600  # 60 minutes

# ── Intraday override cache (2-minute TTL) ───────────────────────────────────
# Used by Fix B: bypass weekly bias filter when price has moved decisively
# in the signal direction (news-driven moves, strong momentum days).
_intraday_override_cache: dict = {}
_INTRADAY_OVERRIDE_TTL_SECS = 120   # 2 min — fresh enough for 5-min cycles
_INTRADAY_OVERRIDE_THRESHOLD = 3.0  # % intraday move (from prior close) required to bypass weekly bias filter
# 3.0% on daily bars: strong enough to signal genuine momentum, rare enough to not fire on normal volatility


def _get_weekly_bias(symbol: str):
    """
    Returns "BULLISH", "BEARISH", or None (data unavailable / bypass).

    Gate rule:
      BULLISH weekly bias → only LONG signals pass.
      BEARISH weekly bias → only SHORT signals pass.
      None               → filter skipped (data unavailable).

    Special cases:
      SQQQ (3x inverse QQQ): uses inverted QQQ weekly bias.
        QQQ BEARISH → SQQQ "BULLISH" (allow LONG SQQQ — bearish market bet)
        QQQ BULLISH → SQQQ "BEARISH" (block LONG SQQQ — QQQ uptrend, wrong environment)
      TSLL, NVDL, TQQQ (other Bucket A): bypass filter entirely (return None).

    Result cached for 60 minutes per symbol.
    """
    # SQQQ: inverted QQQ weekly bias — allow LONG only when QQQ weekly trend is down
    if symbol == "SQQQ":
        qqq_bias = _get_weekly_bias("QQQ")
        if qqq_bias is None:
            return None
        return "BULLISH" if qqq_bias == "BEARISH" else "BEARISH"

    # All other Bucket A tickers (TSLL, NVDL, TQQQ): bypass filter
    if symbol in config.BUCKET_A_TICKERS:
        return None

    now    = datetime.now(ET)
    cached = _weekly_bias_cache.get(symbol)
    if cached and (now - cached["ts"]).total_seconds() < _WEEKLY_BIAS_REFRESH_SECS:
        return cached["bias"]

    try:
        # DATA-1a: T1 Alpaca Data API via data/fetcher.py (replaces yfinance download).
        # Alpaca returns clean lowercase columns — no MultiIndex, no socket timeout wrapper needed.
        wk = fetch_bars(symbol, config.TF_WEEKLY, num_bars=14)

        if wk is None or wk.empty or len(wk) < 12:
            logger.warning(f"[{symbol}] Weekly bias: insufficient data ({len(wk) if wk is not None else 0} bars), filter skipped")
            return None
        closes = wk["close"].dropna()
        if len(closes) < 12:
            logger.warning(f"[{symbol}] Weekly bias: only {len(closes)} weekly bars after dropna, filter skipped")
            return None

        # P2-WEEKLY-BIAS-CONFIRM: require 2 consecutive WEEKLY bars on same side of 10-week SMA.
        # Prevents single-bar whipsaws from flipping a multi-week directional bias.
        # If the two bars disagree (split), hold the prior confirmed bias rather than flipping.
        # On cold start with no prior cache, use the most recent bar's bias (fail-closed).
        sma10      = float(closes.tail(10).mean())
        price_curr = float(closes.values[-1])   # latest completed weekly bar
        price_prev = float(closes.values[-2])   # second-to-last bar

        bias_curr = "BULLISH" if price_curr > sma10 else "BEARISH"
        bias_prev = "BULLISH" if price_prev > sma10 else "BEARISH"

        if bias_curr == bias_prev:
            # Both bars agree — confirmed flip or continuation
            bias = bias_curr
            if cached and cached["bias"] != bias:
                logger.info(f"[{symbol}] Weekly bias CONFIRMED FLIP: {cached['bias']} → {bias} (2-bar agreement above/below SMA10)")
        else:
            # Split signal — hold prior confirmed bias rather than whipsawing.
            # On cold start (no cache), use most recent bar's bias (fail-closed).
            # Returning None would bypass the filter entirely, which is worse than
            # a single-bar approximation.
            bias = cached["bias"] if cached else bias_curr
            logger.debug(
                f"[{symbol}] Weekly bias SPLIT (curr={bias_curr}, prev={bias_prev}) — "
                f"holding {'prior ' + cached['bias'] if cached else 'bias_curr=' + bias_curr + ' (cold start)'}"
            )

        _weekly_bias_cache[symbol] = {"bias": bias, "ts": now}
        return bias
    except Exception as e:
        logger.warning(f"[{symbol}] Weekly bias fetch failed: {e}")
        return None


def _intraday_override(symbol: str, direction: str) -> bool:
    """
    Fix B: Returns True if the symbol's intraday move is >= _INTRADAY_OVERRIDE_THRESHOLD
    in the signal's direction, bypassing the weekly bias filter.

    Logic:
      direction="long"  → override if (current - prev_close) / prev_close >= +3.0%
      direction="short" → override if (current - prev_close) / prev_close <= -3.0%

    Rationale: on news-driven days, a strong intraday move IS the trend.
    The weekly SMA filter is too slow to capture same-session pivots.
    This preserves the filter on calm counter-trend noise while letting
    confirmed momentum through.

    Cached 2 minutes per symbol — avoids redundant fetches during parallel scan.
    """
    now        = datetime.now(ET)
    _cache_key = (symbol, direction)   # direction-specific: long/short moves differ
    cached     = _intraday_override_cache.get(_cache_key)
    if cached and (now - cached["ts"]).total_seconds() < _INTRADAY_OVERRIDE_TTL_SECS:
        pct = cached["pct"]
    else:
        try:
            # DATA-1b: T1 Alpaca Data API via data/fetcher.py (replaces yfinance fast_info).
            # fetch_bars(TF_DAILY, 2) returns yesterday's completed bar + today's
            # in-progress bar during RTH. prev_close = iloc[-2], current = iloc[-1].
            daily = fetch_bars(symbol, config.TF_DAILY, num_bars=2)
            if daily is None or daily.empty or len(daily) < 2:
                return False
            prev_close = float(daily.iloc[-2]["close"])
            current    = float(daily.iloc[-1]["close"])
            if prev_close <= 0 or current <= 0:
                return False
            pct = (current - prev_close) / prev_close * 100
            _intraday_override_cache[_cache_key] = {"pct": pct, "ts": now}
        except Exception:
            return False

    if direction == "long"  and pct >=  _INTRADAY_OVERRIDE_THRESHOLD:
        return True
    if direction == "short" and pct <= -_INTRADAY_OVERRIDE_THRESHOLD:
        return True
    return False



def _analyze_symbol_full(symbol: str, trade_mode: str):
    """
    Fetch + score a single symbol. Returns ALL results (both directions, regardless
    of signal) plus extra data needed for 16pt scoring.
    Returns None on failure.
    """
    try:
        raw_tf = fetch_multi_timeframe(symbol)
        if not raw_tf:
            return None

        tf_data = {tf: prepare_df(df) for tf, df in raw_tf.items()}

        long_result  = score_long_signal(symbol, tf_data, trade_mode)
        short_result = score_short_signal(symbol, tf_data, trade_mode)

        # Numeric momentum_12_1 value for cross-sectional ranking (e.g. 23.5 = 23.5%)
        daily_df    = tf_data.get(config.TF_DAILY)
        mom_summary = get_momentum_summary(daily_df) if daily_df is not None else {}
        mom_12_1_val = mom_summary.get("momentum_12_1")  # numeric pct or None

        # Entry TF dataframe for VWAP SD bands (condition 6)
        entry_tf = config.TF_15M if trade_mode == config.TradeMode.INTRADAY else config.TF_4H
        entry_df = tf_data.get(entry_tf)

        return {
            "symbol":        symbol,
            "long":          long_result,
            "short":         short_result,
            "_mom_12_1_val": mom_12_1_val,
            "_entry_df":     entry_df,
            "_daily_df":     daily_df,
        }

    except Exception as e:
        logger.error(f"[{symbol}] Error during full analysis: {e}", exc_info=True)
        return None


def _get_pead_days(symbol: str, today_str: str) -> tuple:
    """
    Returns (pead_days, pead_direction) for NEW-2 PEAD scoring.
    pead_days: trading days since report (0 = no event in 1-3 day window).
    pead_direction: "long" (beat >5%), "short" (miss <-5%), or "".
    Cache resets daily; FMP JSON cache prevents redundant API calls.
    """
    global _pead_cache_date  # _pead_cache is mutated (dict), not rebound — no global needed
    if _pead_cache_date != today_str:
        _pead_cache.clear()
        _pead_cache_date = today_str

    if symbol in _pead_cache:
        return _pead_cache[symbol]

    result = (0, "")
    try:
        surprise = get_earnings_surprise(symbol)
        if surprise:
            try:
                _today = _date.fromisoformat(today_str)
            except ValueError as _ve:
                logger.warning(f"[{symbol}] _get_pead_days: invalid today_str '{today_str}': {_ve}")
                _pead_cache[symbol] = result
                return result
            _report = surprise["report_date"]
            try:
                if isinstance(_report, str):
                    _report = _date.fromisoformat(_report)
            except ValueError as _ve:
                logger.warning(f"[{symbol}] _get_pead_days: invalid report_date '{_report}': {_ve}")
                _pead_cache[symbol] = result
                return result
            # Count trading days between report and today (weekday-only, good enough for 1-3d window)
            _td_count = 0
            _d = _report
            while _d < _today and _td_count < 10:
                _d += _td(days=1)
                if _d.weekday() < 5:
                    _td_count += 1
            if 1 <= _td_count <= 3:
                _beat = surprise["beat_pct"]
                if _beat > 0.05:
                    result = (_td_count, "long")
                elif _beat < -0.05:
                    result = (_td_count, "short")
    except Exception as _ep:
        logger.warning(f"[{symbol}] _get_pead_days unexpected error: {_ep}")

    _pead_cache[symbol] = result
    return result


def calculate_score_16pt(
    conditions: dict,
    direction: str,
    entry_df,
    daily_df,
    mom_rank,
    universe_size: int,
    trade_mode: str,
    residual_rank=None,   # NEW-3: cross-sectional rank of residual momentum (int or None)
    today_str: str | None = None, # NEW-1: ET date string for FOMC/macro event check
    pead_days: int = 0,    # NEW-2: trading days since EPS beat/miss (0 = no PEAD)
    pead_direction: str = "", # NEW-2: "long" beat, "short" miss, "" = none
) -> dict:
    """
    Compute the 16-point validation score alongside the existing 12-point system.
    LOG ONLY — does not drive any entry/exit decisions.

    Conditions 1-5 are reused from the already-computed 12pt result (no recomputation).
    Conditions 6-9 are new.

    Args:
        conditions:    conditions dict from 12pt score_long/short_signal result
        direction:     "long" or "short"
        entry_df:      prepared entry-TF DataFrame (has vwap column)
        daily_df:      daily DataFrame (for volume, already fetched)
        mom_rank:      int rank 1=best momentum in universe (None if unavailable)
        universe_size: total number of tickers ranked
        trade_mode:    TradeMode.INTRADAY or TradeMode.SWING

    Returns:
        {"score": int, "max_score": 20, "conditions": dict, "notes": list}
    """
    score = 0
    conds: dict = {}
    notes = []

    # ── Conditions 1-5: Reuse from 12pt (zero recomputation) ────────────────
    c1 = bool(conditions.get("daily_above_150sma", False))
    c2 = bool(conditions.get("daily_above_200sma", False))
    c3 = bool(conditions.get("ema13_above_ema30",  False))
    c4 = bool(conditions.get("macd_bullish_cross", False))
    c5 = bool(conditions.get("rsi_in_range",       False))

    conds["c1_daily_150sma"]  = c1
    conds["c2_daily_200sma"]  = c2
    conds["c3_ema_structure"] = c3
    conds["c4_macd_dual_tf"]  = c4
    conds["c5_rsi_filter"]    = c5

    if c1: score += 2
    if c2: score += 2
    if c3: score += 2
    if c4: score += 2
    if c5: score += 1

    # ── Condition 6: VWAP Standard Deviation Bands (2 pts max) ──────────────
    # Replaces binary VWAP check. Measures WHERE price is relative to VWAP±1SD.
    # Long: 2pts = between VWAP and VWAP+1SD (ideal entry). 1pt = below VWAP (discount).
    # Short: mirror logic.
    c6_score = 0
    try:
        if (entry_df is not None
                and "vwap" in entry_df.columns
                and len(entry_df) > 1):
            row   = entry_df.iloc[-1]
            vwap  = row.get("vwap")
            price = row.get("close")
            # NaN check without importing pandas
            if vwap is not None and price is not None and vwap == vwap and price == price:
                deviations = (entry_df["close"] - entry_df["vwap"]).dropna()
                vwap_sd    = float(deviations.std()) if len(deviations) > 1 else 0.0
                upper_1sd  = vwap + vwap_sd
                lower_1sd  = vwap - vwap_sd

                if direction == "long":
                    if vwap <= price <= upper_1sd:      c6_score = 2  # ideal zone
                    elif lower_1sd <= price < vwap:     c6_score = 1  # discount zone
                    # price > upper_1sd (extended) or < lower_1sd (broken) = 0
                else:  # short
                    if lower_1sd <= price <= vwap:      c6_score = 2  # ideal short zone
                    elif vwap < price <= upper_1sd:     c6_score = 1  # mild extension
                    # price < lower_1sd (collapsed) or > upper_1sd (too extended) = 0
            else:
                c6_score = 1  # VWAP value missing — neutral fallback
                notes.append("c6_vwap_sd: no vwap value, scored 1 (neutral)")
        else:
            c6_score = 1   # swing mode or no entry_df — neutral
            notes.append("c6_vwap_sd: no entry_df, scored 1 (neutral)")
    except Exception as e:
        c6_score = 1
        notes.append(f"c6_vwap_sd: error ({e}), scored 1 (neutral)")

    conds["c6_vwap_sd_bands"] = c6_score
    score += c6_score

    # ── Condition 7: Cross-Sectional Momentum Rank (2 pts max) ───────────────
    # NEW-3: residual_rank (sector-adjusted) replaces mom_rank when available.
    # Residual momentum strips sector beta for a cleaner stock-specific signal (§3.7).
    # Long: 2pts if top quartile. 1pt if top half. 0 otherwise.
    # Short: mirror logic (high rank number = most negative residual = short edge).
    c7_score = 0
    _rank_used  = residual_rank if residual_rank is not None else mom_rank
    _rank_label = "residual_rank" if residual_rank is not None else "mom_rank"
    try:
        if _rank_used is not None and universe_size > 0:
            top_q    = max(1, universe_size // 4)
            top_half = max(1, universe_size // 2)
            bot_q    = universe_size - (universe_size // 4) + 1

            if direction == "long":
                if _rank_used <= top_q:      c7_score = 2
                elif _rank_used <= top_half: c7_score = 1
            else:
                if _rank_used >= bot_q:      c7_score = 2
                elif _rank_used > top_half:  c7_score = 1
            notes.append(f"c7_{_rank_label}: rank={_rank_used}/{universe_size} → {c7_score}pt")
        else:
            notes.append("c7_momentum_rank: unavailable, scored 0")
    except Exception as e:
        notes.append(f"c7_momentum_rank: error ({e}), scored 0")

    conds["c7_momentum_rank"] = c7_score
    score += c7_score

    # ── Condition 8: Relative Volume (2 pts max) ─────────────────────────────
    # Compares current bar volume to 20-day average. Reuses daily_df — no new API calls.
    # 2pts >= 1.8x avg. 1pt >= 1.2x. 0pts below 1.2x.
    c8_score = 0
    try:
        if (daily_df is not None
                and len(daily_df) >= 21
                and "volume" in daily_df.columns):
            avg_vol = float(daily_df["volume"].tail(20).mean())
            cur_vol = float(daily_df["volume"].iloc[-1])
            if avg_vol > 0:
                vol_ratio = cur_vol / avg_vol
                if vol_ratio >= 1.8:    c8_score = 2
                elif vol_ratio >= 1.2:  c8_score = 1
        else:
            notes.append("c8_rel_volume: insufficient data, scored 0")
    except Exception as e:
        notes.append(f"c8_rel_volume: error ({e}), scored 0")

    conds["c8_rel_volume"] = c8_score
    score += c8_score

    # ── Condition 9: Implied Range Position (1 pt max) ───────────────────────
    # Conditional on SPY implied range data from scan_to_html.py header build.
    # Not yet available in signal_generator context — always 0 until wired up.
    conds["c9_implied_range"] = 0
    notes.append("c9_implied_range: data pending, scored 0")
    # score += 0

    # ── Condition 10: IBS — Internal Bar Strength (1 pt, log-only, board-approved 2026-04-20) ──
    # IBS = (close - low) / (high - low) — measures bar's close position within its range.
    # Long: IBS < 0.25 (close near low) → mean-reversion edge → +1
    # Short: IBS > 0.75 (close near high) → +1
    # Uses current daily bar (iloc[-1]). Requires H ≠ L.
    c10_score = 0
    try:
        if daily_df is not None and len(daily_df) >= 2:
            _row = daily_df.iloc[-1]
            _h = float(_row["high"])  if "high"  in daily_df.columns else None
            _l = float(_row["low"])   if "low"   in daily_df.columns else None
            _c = float(_row["close"]) if "close" in daily_df.columns else None
            if _h is not None and _l is not None and _c is not None and (_h - _l) > 0:
                _ibs = (_c - _l) / (_h - _l)
                if direction == "long"  and _ibs < 0.25:
                    c10_score = 1
                elif direction == "short" and _ibs > 0.75:
                    c10_score = 1
                notes.append(f"c10_ibs: {_ibs:.3f} → {c10_score}pt")
            else:
                notes.append("c10_ibs: OHLC missing or zero range")
        else:
            notes.append("c10_ibs: insufficient daily_df")
    except Exception as _e10:
        notes.append(f"c10_ibs: error ({_e10})")

    conds["c10_ibs"] = c10_score
    score += c10_score

    # ── Condition 11: Pivot S/R proximity (1 pt, log-only, board-approved 2026-04-20) ──
    # Pivot computed from prior day OHLC: C=(PH+PL+PC)/3; S1=2C-PH; R1=2C-PL
    # Long: current close within 0.5×ATR_proxy of S1 → +1
    # Short: current close within 0.5×ATR_proxy of R1 → +1
    # ATR proxy = prior day's H-L range.
    c11_score = 0
    try:
        if daily_df is not None and len(daily_df) >= 2:
            _prev = daily_df.iloc[-2]
            _curr = daily_df.iloc[-1]
            _pH = float(_prev["high"])  if "high"  in daily_df.columns else None
            _pL = float(_prev["low"])   if "low"   in daily_df.columns else None
            _pC = float(_prev["close"]) if "close" in daily_df.columns else None
            _cur_close = float(_curr["close"]) if "close" in daily_df.columns else None
            if _pH is not None and _pL is not None and _pC is not None and _cur_close is not None and _pH > _pL > 0:
                _piv  = (_pH + _pL + _pC) / 3
                _R1   = 2 * _piv - _pL
                _S1   = 2 * _piv - _pH
                _thresh = 0.5 * (_pH - _pL)
                if direction == "long"  and abs(_cur_close - _S1) <= _thresh:
                    c11_score = 1
                    notes.append(f"c11_pivot: near S1=${_S1:.2f} close=${_cur_close:.2f} → 1pt")
                elif direction == "short" and abs(_cur_close - _R1) <= _thresh:
                    c11_score = 1
                    notes.append(f"c11_pivot: near R1=${_R1:.2f} close=${_cur_close:.2f} → 1pt")
                else:
                    notes.append(f"c11_pivot: S1=${_S1:.2f} R1=${_R1:.2f} close=${_cur_close:.2f} → 0pt")
            else:
                notes.append("c11_pivot: prior day OHLC missing")
        else:
            notes.append("c11_pivot: insufficient daily_df")
    except Exception as _e11:
        notes.append(f"c11_pivot: error ({_e11})")

    conds["c11_pivot_sr"] = c11_score
    score += c11_score

    # ── Condition 12: FOMC/Macro Day Boost (1 pt, LONG only, log-only, board-approved 2026-04-20) ──
    # §19.5: equities yield higher-than-average returns on FOMC/CPI/NFP announcement days.
    # LONG only — no statistical short edge on announcement dates.
    c12_score = 0
    try:
        _is_macro = is_macro_event_day(today_str)
        if _is_macro and direction == "long":
            c12_score = 1
            notes.append("c12_fomc_day: macro event day → 1pt LONG boost")
        else:
            notes.append(f"c12_fomc_day: {'macro day/short direction' if _is_macro else 'not macro day'} → 0pt")
    except Exception as _e12:
        notes.append(f"c12_fomc_day: error ({_e12})")

    conds["c12_fomc_day"] = c12_score
    score += c12_score

    # ── Condition 13: PEAD — Post-Earnings Announcement Drift (1 pt, log-only, board-approved 2026-04-20) ──
    # §3.2: within 1-3 trading days of EPS beat >5% → LONG drift edge.
    # Within 1-3 trading days of EPS miss → SHORT drift edge.
    # pead_days=0 means no recent earnings event.
    c13_score = 0
    try:
        if 1 <= pead_days <= 3 and pead_direction:
            if pead_direction == "long" and direction == "long":
                c13_score = 1
                notes.append(f"c13_pead: EPS beat day+{pead_days} LONG → 1pt")
            elif pead_direction == "short" and direction == "short":
                c13_score = 1
                notes.append(f"c13_pead: EPS miss day+{pead_days} SHORT → 1pt")
            else:
                notes.append(f"c13_pead: PEAD {pead_direction} vs direction={direction} → 0pt")
        else:
            notes.append(f"c13_pead: no PEAD event (days={pead_days}) → 0pt")
    except Exception as _e13:
        notes.append(f"c13_pead: error ({_e13})")

    conds["c13_pead"] = c13_score
    score += c13_score

    return {
        "score":      score,
        "max_score":  SCORE_16PT_MAX,
        "conditions": conds,
        "notes":      notes,
    }


def run_scan(
    trade_mode: str = config.TradeMode.INTRADAY,
    max_workers: int = 2,
    news_size_mult: float = 1.0,
) -> list:
    """
    Scan the full universe and return ranked signals.
    max_workers=2 — 23 symbols × 3 TFs = 69 requests; at 2 workers peak burst is ~34/min,
    well under the Alpaca free-tier 200 req/min limit. max_workers=4 exceeded the cap
    (276 req/min) and caused 429s that the backoff couldn't fully absorb.

    Now runs in three phases:
      Phase 1: Full analysis for all symbols (12pt scoring + data collection)
      Phase 2: Build cross-sectional momentum rank across universe
      Phase 3: Compute 16pt scores, filter to 12pt signals, write comparison log

    Returns:
        List of signal dicts, sorted by score descending.
        Each dict includes score_16pt and score_16pt_max (log only).
    """
    # HALT: breaking news so severe no new entries should fire at all
    if news_size_mult == 0.0:
        logger.warning("NEWS HALT active — suppressing all new entry signals")
        return []

    universe = build_scan_universe(run_atr_filter=True)
    if not universe:
        logger.warning("build_scan_universe returned empty — falling back to full universe")
        universe = get_universe()
    if not universe:
        logger.error("Empty universe — check internet connection.")
        return []

    logger.info(f"Scanning {len(universe)} tickers in {trade_mode} mode...")

    # ── Phase 0: Weekly bias pre-fetch (cached 60 min, ~4s first run) ─────────
    weekly_biases: dict = {}
    for sym in universe:
        weekly_biases[sym] = _get_weekly_bias(sym)

    # ── Phase 1: Full analysis for all symbols ────────────────────────────────
    full_results = []  # list of _analyze_symbol_full() dicts

    # HANG FIX: Do NOT use `with ThreadPoolExecutor` — its __exit__ calls
    # shutdown(wait=True) which blocks until ALL threads complete. If any
    # fetch_bars() call stalls on a pooled Alpaca connection, the with-block
    # hangs indefinitely and the watchdog fires. Use explicit shutdown(wait=False)
    # to abandon stuck threads after the timeout, same pattern as news_monitor.
    # Timeout: 23 tickers / 4 workers ≈ 18s normal; 90s covers 429 backoff storms.
    _scan_pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scan")
    _scan_futures = {
        _scan_pool.submit(_analyze_symbol_full, symbol, trade_mode): symbol
        for symbol in universe
    }
    try:
        for future in as_completed(_scan_futures, timeout=90):
            try:
                result = future.result(timeout=20)
            except _FutTimeout:
                logger.warning(f"run_scan: per-future timeout — {_scan_futures[future]} stuck >20s, skipping")
                continue
            if result is not None:
                full_results.append(result)
    except _FutTimeout:
        _stuck = [_scan_futures[f] for f in _scan_futures if not f.done()]
        logger.warning(f"run_scan: phase-1 global timeout — abandoned symbols: {_stuck}")
    finally:
        _scan_pool.shutdown(wait=False, cancel_futures=True)

    # ── Phase 2: Cross-sectional momentum rank ────────────────────────────────
    # Collect numeric momentum_12_1 values for all symbols that have them
    mom_vals = {}
    for fr in full_results:
        val = fr["_mom_12_1_val"]
        if val is not None:
            mom_vals[fr["symbol"]] = val

    # Rank descending: rank 1 = highest momentum (best for longs)
    ranked_syms = sorted(mom_vals, key=lambda s: mom_vals[s], reverse=True)
    universe_size = len(full_results)
    mom_ranks = {sym: (i + 1) for i, sym in enumerate(ranked_syms)}

    # ── Phase 2b: Residual momentum ranks (sector-adjusted, NEW-3) ───────────
    # ε_i = mom_12_1(stock) - avg(mom_12_1(sector_peers))
    # Strips sector beta; rank 1 = highest idiosyncratic upward momentum (§3.7).
    residual_ranks: dict = {}
    try:
        _sector_moms: dict = defaultdict(list)
        for _fr in full_results:
            _v = _fr["_mom_12_1_val"]
            _sec = _SECTOR_MAP_SG.get(_fr["symbol"], "unknown")
            if _v is not None:
                _sector_moms[_sec].append(_v)
        _sector_avg = {s: sum(vs) / len(vs) for s, vs in _sector_moms.items() if vs}
        _residuals: dict = {}
        for _fr in full_results:
            _v = _fr["_mom_12_1_val"]
            if _v is not None:
                _sec = _SECTOR_MAP_SG.get(_fr["symbol"], "unknown")
                _residuals[_fr["symbol"]] = _v - _sector_avg.get(_sec, 0.0)
        _ranked_res = sorted(_residuals, key=lambda s: _residuals[s], reverse=True)
        residual_ranks = {sym: (i + 1) for i, sym in enumerate(_ranked_res)}
    except Exception as _rr_e:
        logger.debug(f"run_scan: residual rank error: {_rr_e}")

    # ── Phase 3: 16pt scoring, signal filtering, comparison log ──────────────
    _now_et        = datetime.now(ET)          # single clock reading for entire Phase 3
    _today_p3      = _now_et.strftime("%Y-%m-%d")
    all_signals    = []
    score_comparison = []

    for fr in full_results:
        sym      = fr["symbol"]
        long_r   = fr["long"]
        short_r  = fr["short"]
        entry_df = fr["_entry_df"]
        daily_df = fr["_daily_df"]
        rank     = mom_ranks.get(sym)

        # ── VOTE-3/5: vol_ratio + sigma_20d; ADDV floor (board-approved 2026-04-20) ──
        _vol_ratio = None
        _sigma_20d = None
        _addv_ok   = True
        if daily_df is not None and len(daily_df) >= 21:
            try:
                if "volume" in daily_df.columns and "close" in daily_df.columns:
                    _avg_vol  = float(daily_df["volume"].tail(20).mean())
                    _cur_vol  = float(daily_df["volume"].iloc[-1])
                    _avg_px   = float(daily_df["close"].tail(20).mean())
                    if _avg_vol > 0:
                        _vol_ratio = _cur_vol / _avg_vol
                        _addv = _avg_vol * _avg_px
                        if _addv < 50_000_000:   # ADDV floor: $50M avg daily dollar vol
                            _addv_ok = False
                            logger.debug(f"[{sym}] ADDV ${_addv/1e6:.0f}M < $50M — excluded from signals")
                if "close" in daily_df.columns:
                    _rets = daily_df["close"].pct_change().dropna().tail(20)
                    if len(_rets) >= 20:
                        _sigma_20d = float(_rets.std() * (252 ** 0.5))
            except Exception as _ve:
                logger.debug(f"[{sym}] vol/sigma/ADDV compute error: {_ve}")

        if not _addv_ok:
            fr.pop("_entry_df", None)
            fr.pop("_daily_df", None)
            continue   # skip below-liquidity symbols entirely

        long_r["vol_ratio"]  = _vol_ratio
        long_r["sigma_20d"]  = _sigma_20d
        short_r["vol_ratio"] = _vol_ratio
        short_r["sigma_20d"] = _sigma_20d

        # Compute 16pt for both directions using pre-ranked momentum + residual + PEAD + macro
        _pead_days_16, _pead_dir_16 = _get_pead_days(sym, _today_p3)
        _res_rank_16   = residual_ranks.get(sym)

        long_16  = calculate_score_16pt(
            conditions=long_r["conditions"],
            direction="long",
            entry_df=entry_df,
            daily_df=daily_df,
            mom_rank=rank,
            universe_size=universe_size,
            trade_mode=trade_mode,
            residual_rank=_res_rank_16,
            today_str=_today_p3,
            pead_days=_pead_days_16,
            pead_direction=_pead_dir_16,
        )
        short_16 = calculate_score_16pt(
            conditions=short_r["conditions"],
            direction="short",
            entry_df=entry_df,
            daily_df=daily_df,
            mom_rank=rank,
            universe_size=universe_size,
            trade_mode=trade_mode,
            residual_rank=_res_rank_16,
            today_str=_today_p3,
            pead_days=_pead_days_16,
            pead_direction=_pead_dir_16,
        )

        # Free DataFrame refs after 16pt scoring (Priority 4 RAM leak fix — DS+GAI Q3/Q5, S43B)
        entry_df = fr.pop("_entry_df", None)
        daily_df = fr.pop("_daily_df", None)
        del entry_df, daily_df

        # Tag 16pt scores onto signal dicts for downstream logging
        long_r["score_16pt"]      = long_16["score"]
        long_r["score_16pt_max"]  = long_16["max_score"]
        short_r["score_16pt"]     = short_16["score"]
        short_r["score_16pt_max"] = short_16["max_score"]

        # Score comparison record
        dir_12 = "long" if long_r["score"] >= short_r["score"] else "short"
        dir_16 = "long" if long_16["score"] >= short_16["score"] else "short"
        long_16_signal  = long_16["score"]  >= SCORE_16PT_MIN
        short_16_signal = short_16["score"] >= SCORE_16PT_MIN

        score_comparison.append({
            "symbol":           sym,
            "long_12pt":        long_r["score"],
            "short_12pt":       short_r["score"],
            "long_signal_12":   long_r["signal"],
            "short_signal_12":  short_r["signal"],
            "long_16pt":        long_16["score"],
            "short_16pt":       short_16["score"],
            "long_signal_16":   long_16_signal,
            "short_signal_16":  short_16_signal,
            "direction_agreement": dir_12 == dir_16,
            "16pt_would_differ": (long_16_signal != long_r["signal"]
                                  or short_16_signal != short_r["signal"]),
            "mom_rank":         rank,
            "16pt_notes":       long_16["notes"],
        })

        # ── Weekly bias gate (mandatory pre-filter, not a scoring point) ────
        wb = weekly_biases.get(sym)
        long_r["weekly_bias"]           = wb
        short_r["weekly_bias"]          = wb
        long_r["weekly_bias_filtered"]  = False
        short_r["weekly_bias_filtered"] = False

        if wb is not None:
            if wb == "BEARISH" and long_r["signal"]:
                if _intraday_override(sym, "long"):
                    logger.info(
                        f"[{sym}] Weekly bias BEARISH overridden — "
                        f"strong intraday long move (>={_INTRADAY_OVERRIDE_THRESHOLD}%)"
                    )
                else:
                    long_r["weekly_bias_filtered"] = True
                    logger.info(f"[{sym}] LONG signal filtered by weekly bias (BEARISH)")
            if wb == "BULLISH" and short_r["signal"]:
                if _intraday_override(sym, "short"):
                    logger.info(
                        f"[{sym}] Weekly bias BULLISH overridden — "
                        f"strong intraday short move (>={_INTRADAY_OVERRIDE_THRESHOLD}%)"
                    )
                else:
                    short_r["weekly_bias_filtered"] = True
                    logger.info(f"[{sym}] SHORT signal filtered by weekly bias (BULLISH)")

        # Filter to 12pt signals — existing behavior, 16pt is log only
        # Weekly-bias-filtered signals are excluded from entries
        if long_r["signal"] and not long_r["weekly_bias_filtered"]:
            all_signals.append(long_r)
        if short_r["signal"] and not short_r["weekly_bias_filtered"]:
            all_signals.append(short_r)

    # ── Write score comparison log (overwritten each scan cycle) ─────────────
    try:
        _LOGS_DIR.mkdir(exist_ok=True)
        comp_path = _LOGS_DIR / f"score_comparison_{_today_p3}.json"
        with open(comp_path, "w") as f:
            json.dump({
                "date":        _today_p3,
                "scan_time":   _now_et.isoformat(),
                "trade_mode":  trade_mode,
                "universe":    universe_size,
                "tickers":     score_comparison,
            }, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Score comparison log write failed: {e}")

    # Prune score_comparison files older than 14 days to prevent unbounded accumulation
    try:
        _sc_cutoff = (_now_et - _td(days=14)).strftime("%Y-%m-%d")
        for _sc_old in glob.glob(str(_LOGS_DIR / "score_comparison_*.json")):
            _sc_date = Path(_sc_old).stem.replace("score_comparison_", "")
            if _sc_date < _sc_cutoff:
                Path(_sc_old).unlink(missing_ok=True)
    except Exception as _e:
        logger.warning(f"run_scan: score_comparison prune error: {_e}")

    # ── Sort + dedup (existing behavior unchanged) ────────────────────────────
    all_signals.sort(key=lambda x: x["score"], reverse=True)

    seen_symbols = set()
    deduped = []
    for sig in all_signals:
        if sig["symbol"] not in seen_symbols:
            deduped.append(sig)
            seen_symbols.add(sig["symbol"])

    if len(deduped) < len(all_signals):
        logger.info(
            f"Deduped {len(all_signals) - len(deduped)} conflicting signals "
            f"(kept highest score per symbol)"
        )

    logger.info(f"Scan complete. {len(deduped)} signals after dedup (from {len(all_signals)} raw).")
    return deduped


def generate_signal(
    symbol:     str,
    tf_data:    dict,
    direction:  str = "long",
    trade_mode: str = config.TradeMode.INTRADAY,
) -> dict:
    """
    Confluence signal for the movers bot scanner.
    Called by strategy/movers/scanner.add_confluence_score().

    Args:
        symbol:     Ticker symbol
        tf_data:    {timeframe_str: DataFrame} -- raw bars (TF_WEEKLY accepted, unused by scorer)
        direction:  "long" or "short" -- which side to score
        trade_mode: TradeMode.INTRADAY (default); swing mode requires TF_4H which is not
                    currently in TIMEFRAMES_FOR_CONFLUENCE -- use INTRADAY until TF_4H is added

    Returns:
        {"score": int, "trend": {"bias": str}}   bias: "bullish"|"bearish"|"neutral"
    """
    try:
        if not isinstance(tf_data, dict) or not tf_data:
            logger.warning(f"[{symbol}] generate_signal: empty or invalid tf_data")
            return {"score": 0, "trend": {"bias": "neutral"}}

        prepared = {
            tf: prepare_df(df.copy())
            for tf, df in tf_data.items()
            if df is not None and not df.empty
        }
        if not prepared:
            logger.warning(
                f"[{symbol}] generate_signal: all DataFrames None/empty after prepare_df -- score=0"
            )
            return {"score": 0, "trend": {"bias": "neutral"}}

        if direction == "short":
            result = score_short_signal(symbol, prepared, trade_mode)
        else:
            result = score_long_signal(symbol, prepared, trade_mode)

        score    = result.get("score", 0)
        raw_bias = result.get("bias", "unknown")
        bias     = raw_bias if raw_bias not in ("unknown", "", None) else "neutral"
        return {"score": score, "trend": {"bias": bias}}

    except Exception as e:
        logger.warning(f"[{symbol}] generate_signal error: {e}")
        return {"score": 0, "trend": {"bias": "neutral"}}


def get_exit_signal(symbol: str, direction: str, trade_mode: str) -> bool:
    """
    Check if an open position should be exited.
    Exit when the original entry thesis breaks down (MACD flip or EMA cross reversal).
    Logs which specific indicator(s) triggered for transparency in reversal counter display.
    """
    try:
        raw_tf = fetch_multi_timeframe(symbol)
        tf_data = {tf: prepare_df(df) for tf, df in raw_tf.items()}

        entry_tf = config.TF_15M if trade_mode == config.TradeMode.INTRADAY else config.TF_4H
        entry_df = tf_data.get(entry_tf)

        if entry_df is None:
            return False

        if direction == "long":
            macd_fired = macd_bearish_cross(entry_df, config.MACD_FAST)
            ema_fired  = ema_bearish_cross(entry_df)
            triggered  = macd_fired or ema_fired
            if triggered:
                reasons = []
                if macd_fired: reasons.append("MACD bearish cross")
                if ema_fired:  reasons.append("EMA 13×30 bearish cross")
                logger.info(f"[{symbol}] Exit indicator(s) on {entry_tf}: {' + '.join(reasons)}")
            return triggered
        else:
            macd_fired = macd_bullish_cross(entry_df, config.MACD_FAST)
            ema_fired  = ema_bullish_cross(entry_df)
            triggered  = macd_fired or ema_fired
            if triggered:
                reasons = []
                if macd_fired: reasons.append("MACD bullish cross")
                if ema_fired:  reasons.append("EMA 13×30 bullish cross")
                logger.info(f"[{symbol}] Exit indicator(s) on {entry_tf}: {' + '.join(reasons)}")
            return triggered

    except Exception as e:
        logger.warning(f"[{symbol}] Exit check error: {e}")
        return False
