# ruff: noqa: E501, E701, E702  — pre-existing long lines / inline semicolons from main.py body
"""
strategy/run_cycle.py — Phase 2 Extraction 9

Owns the full scan cycle: SPY gate check, pre-market refresh, breadth/regime
load, signal generation, and orchestration of entries/exits.

All main.py module-level globals are accessed via lazy ``import main as _main``
inside the function body (circular-import avoidance). Functions still in
main.py (execute_entries, check_exits, etc.) are called the same way.
Phase 3 will break those remaining dependencies once the full body move is done.
"""
from __future__ import annotations

import gc
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, date  # noqa: F401
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import config
import data.bar_cache as _bar_cache  # noqa: F401 — used inline inside function body
from alerts import alert_kill_switch, alert_spy_event, send_slack
from data.fetcher import fetch_bars
from data.sectors import SECTOR_MAP as _SECTOR_MAP  # noqa: F401
from execution.broker import (
    cancel_order,
    get_account,
    get_open_position,
    get_order,
    get_portfolio_value,
    is_market_open,
    submit_gtc_stop_order,
    submit_limit_order,
)
from execution.fill_helpers import (
    fetch_actual_fill_price as _fetch_actual_fill_price,
    get_fill_fallback_count as _get_fill_fallback_count,
)
from execution.fill_reconciler import run_fill_reconciliation as _run_fill_recon
from execution.lifecycle import (
    apply_mri_breakeven_push as _apply_mri_breakeven_push,
    get_shorts_banned as _get_shorts_banned,
    get_shorting_enabled as _get_shorting_enabled,
    set_shorting_enabled as _set_shorting_enabled,
)
from execution.orphan_manager import (
    cancel_and_reconcile_gtc_stops as _cancel_and_reconcile_gtc_stops,
    get_tod_phase as _get_tod_phase,
    tod_size_multiplier as _tod_size_multiplier,
)
from monitoring.watchdog import touch_cycle_ts as _touch_cycle_ts
from strategy.scoring import clear_live_score_cache as _clear_live_score_cache
from strategy.signal_generator import run_scan, SCORE_16PT_MIN as _16PT_MIN
from trade_logger import log_event as _log_trade_event

if TYPE_CHECKING:
    from events.calendar import EventCalendar
    from events.macro_risk_index import MacroRiskIndex
    from events.news_monitor import NewsMonitor
    from execution.kelly import KellySizer
    from execution.portfolio_tracker import PortfolioTracker
    from execution.risk_manager import RiskManager
    from main import GateState
    from strategy.volatility_regime import RegimeDetector

ET = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent  # RC-2: this file lives in strategy/; root is one level up

# ── SPY 52w-high persistence — ATH fail-open fix (board + Gro + GAI, 2026-07-01) ──
# Previously: a fetch failure left _main._spy_52w_high at its 0.0 default for the
# rest of the session, silently disabling the ATH MIN_SCORE floor-raise (this file,
# Layer 5 below) AND the risk_manager 0.90x ATH stop/target scalar — the exact
# opposite of the ORB gate's fail-CLOSED posture on the same class of failure.
# A 52w high moves negligibly day-to-day, so the last-known value is a far better
# estimate than "assume not near ATH." Board (Derman/Taleb, Thorp/Harris — all 4
# APPROVE): persist + reuse on failure; true bootstrap (no cache ever) defaults
# CONSERVATIVE (assume near-ATH) rather than fail-open.
_SPY_52W_HIGH_CACHE = _PROJECT_ROOT / "data" / "state" / "spy_52w_high.json"


def _save_spy_52w_high(value: float) -> None:
    """Atomically persist today's SPY 52w high for fail-safe reuse on fetch failure."""
    try:
        _SPY_52W_HIGH_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _payload = {"value": value, "date": datetime.now(ET).strftime("%Y-%m-%d")}
        _tmp = _SPY_52W_HIGH_CACHE.with_suffix(".tmp")
        with open(_tmp, "w") as _f:
            json.dump(_payload, _f)
        os.replace(_tmp, _SPY_52W_HIGH_CACHE)
    except Exception as _save_err:
        logger.warning("SPY 52w high cache save failed (non-critical): %s", _save_err)


def _load_spy_52w_high() -> dict | None:
    """Return {"value": float, "age_days": int} from the last successful fetch, or None."""
    try:
        if not _SPY_52W_HIGH_CACHE.exists():
            return None
        _raw = json.loads(_SPY_52W_HIGH_CACHE.read_text())
        _saved_date = date.fromisoformat(_raw["date"])
        _age_days = (datetime.now(ET).date() - _saved_date).days
        return {"value": float(_raw["value"]), "age_days": _age_days}
    except Exception as _load_err:
        logger.warning("SPY 52w high cache load failed: %s", _load_err)
        return None


def run_cycle(
    risk: RiskManager,
    tracker: PortfolioTracker,
    calendar: EventCalendar,
    regime: "RegimeDetector",
    kelly: "KellySizer",
    news: "NewsMonitor",
    mri: "MacroRiskIndex",
    trade_mode: str,
    universe_override: "list | None" = None,
    gate_state: "GateState | None" = None,
):
    if gate_state is None:
        raise RuntimeError(
            "run_cycle() called without gate_state — Phase 0.5 refactor incomplete"
        )
    import main as _main  # lazy — avoids circular import at module load
    # Global declarations first — Python requires globals to be declared before
    # any use within the function scope.
    # (globals accessed via _main)
    # (globals accessed via _main)
    _clear_live_score_cache()   # P1-SCORE-CACHE: Phase 2 — strategy/scoring.py

    now = datetime.now(ET)
    tod_phase = _get_tod_phase(now)
    _hdr_spy  = f" | SPY:{_main._spy_event_type}" if _main._spy_event_type else ""
    _hdr_tqi  = f" | TQI:{sum(kelly._tqi_history)/len(kelly._tqi_history):.0f}" if kelly._tqi_history else ""
    logger.info(
        f"─── MTF cycle @ {now.strftime('%H:%M:%S')} ET | "
        f"Mode: {trade_mode} | TOD: {tod_phase}{_hdr_spy}{_hdr_tqi} ───"
    )

    # ── Build #9: Restore hybrid engine state on first cycle after restart ──────
    if not _main._hybrid_state_loaded:
        _main._load_hybrid_state()

    # ── Startup R-GUARD: trigger reconcile_eod.py if post-market and EOD has no sentinel ──
    # Handles crash-recovery: bot crashed before the 4:10 PM ET cron fired.
    # Fires once per process lifetime (_startup_reconcile_checked on function object).
    # Weekday guard intentionally omitted — file-existence check prevents spurious
    # triggers on weekends/early-morning restarts where no EOD file exists yet.
    if not getattr(run_cycle, "_startup_reconcile_checked", False):
        run_cycle._startup_reconcile_checked = True  # type: ignore[attr-defined]
        try:
            _sr_now_et = datetime.now(ZoneInfo("America/New_York"))
            _sr_today  = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
            _sr_eod    = _PROJECT_ROOT / "logs" / f"eod_{_sr_today}.json"
            _sr_post_mkt = (
                (_sr_now_et.hour > 16) or (_sr_now_et.hour == 16 and _sr_now_et.minute >= 10)
            )
            if _sr_post_mkt and _sr_eod.exists():
                try:
                    _sr_data = json.loads(_sr_eod.read_text())
                except Exception as _sr_e:
                    _sr_data = {}
                    logger.warning(
                        "Startup reconcile check: EOD JSON parse failed — %s", _sr_e
                    )
                if not _sr_data.get("_reconcile_ts"):
                    logger.info(
                        "Startup R-GUARD: eod_%s has no _reconcile_ts — triggering reconcile_eod.py",
                        _sr_today,
                    )
                    import subprocess as _sr_sp
                    _sr_sp.Popen(
                        [sys.executable, str(_PROJECT_ROOT / "reconcile_eod.py")],
                        stdout=_sr_sp.DEVNULL, stderr=_sr_sp.DEVNULL,
                    )
        except Exception as _sr_e:
            logger.warning(f"Startup reconcile check failed (non-fatal): {_sr_e}")

    # ── Kill switch check ────────────────────────────────────────────────────────────────────────────
    portfolio_value = get_portfolio_value()
    risk.update_portfolio_value(portfolio_value)

    if risk.check_kill_switch():
        logger.critical("Kill switch active — no new entries this session.")
        if not _main._kill_switch_alerted:
            _main._kill_switch_alerted = True
            _ks_pnl = getattr(risk, "daily_pnl", 0.0)
            _ks_pct = getattr(config, "MAX_DAILY_LOSS_PCT", 0.05)
            alert_kill_switch(daily_pnl=_ks_pnl, limit_pct=_ks_pct,
                              portfolio=portfolio_value)
        _touch_cycle_ts()
        return

    # ── VOTE-4: SPY 200d MA — once-per-day refresh (board-approved 2026-04-20) ─
    # Used in _main.execute_entries() to halve overnight size when SPY < 200d MA.
    # Fetches 210 daily bars (200 needed + buffer). Refreshes once per ET calendar date.
    _today_date_str = now.strftime("%Y-%m-%d")
    if _main._spy_200d_ma_date != _today_date_str:
        try:
            from data.fetcher import fetch_bars as _fb_200
            _spy_daily_200 = _fb_200("SPY", config.TF_DAILY, num_bars=210)
            if _spy_daily_200 is not None and len(_spy_daily_200) >= 200:
                _main._spy_200d_ma      = float(_spy_daily_200["close"].tail(200).mean())
                _main._spy_200d_ma_date = _today_date_str
                logger.info(
                    f"VOTE-4: SPY 200d MA = ${_main._spy_200d_ma:.2f} "
                    f"(computed from {len(_spy_daily_200)} daily bars, "
                    f"SPY last close ${float(_spy_daily_200['close'].iloc[-1]):.2f})"
                )
            else:
                logger.warning(
                    f"VOTE-4: SPY 200d MA fetch insufficient bars "
                    f"({len(_spy_daily_200) if _spy_daily_200 is not None else 0}) — skipping"
                )
        except Exception as _e200:
            logger.warning(f"VOTE-4: SPY 200d MA refresh failed: {_e200}")

    # ── Pre-market phase — runs before market open ──────────────────────────────────────────────
    if tod_phase == "premarket":
        logger.info("Pre-market window (8:45–9:30 ET) — scanning movers, no entries.")

        # ── Cancel overnight GTC stops so they don't appear on L2 during RTH ──
        # Also reconcile: if a GTC stop was triggered overnight, close tracker record.
        _cancel_and_reconcile_gtc_stops(tracker, risk)   # C-2: pass risk

        from data.premarket import scan_premarket_movers, cache_premarket_movers
        from concurrent.futures import ThreadPoolExecutor as _PmTPE
        movers = []
        try:
            # 90s hard timeout — scan_premarket_movers() makes 150+ sequential API
            # calls and can hang indefinitely on a stalled TCP connection. If it
            # exceeds 90s the main thread unblocks and continues; the worker thread
            # will self-terminate once socket.setdefaulttimeout(60) expires its conn.
            with _PmTPE(max_workers=1) as _pm_ex:
                _pm_fut = _pm_ex.submit(scan_premarket_movers)
                movers = _pm_fut.result(timeout=90)
        except Exception as _pm_err:
            logger.warning(
                f"Pre-market mover scan failed or timed out ({_pm_err}) — "
                f"skipping movers this session, proceeding with core watchlist only."
            )
            movers = []
        if movers:
            # (globals accessed via _main)
            _main._premarket_movers_cache = movers
            cache_premarket_movers(movers)    # H-2: persist into data.premarket module
            logger.info(f"Pre-market movers cached ({len(movers)}): {movers}")

        # ── Pre-market data refresh: breadth, earnings batch, analyst sentiment ──
        # Runs once per pre-market window (8:45–9:30 ET). All three are T2/T3
        # with caches so repeated pre-market scans are cheap (cache hits).
        # (globals accessed via _main)
        try:
            from data.market_breadth import get_breadth_score
            _main._last_breadth = get_breadth_score()
            _log_trade_event(
                "breadth_refresh", symbol="BREADTH", price=0.0, size=0,
                mri_level=mri.level() if mri else "NORMAL", score=_main._last_breadth["score"],
                data_source="tradermonty_csv",
                zone=_main._last_breadth["zone"], breadth_8ma=_main._last_breadth.get("breadth_8ma", 0),
                bearish_signal=_main._last_breadth.get("bearish_signal", False),
            )
        except Exception as _br_err:
            logger.warning(f"Pre-market breadth refresh failed: {_br_err}")

        # ── SPY 52-week high refresh (T1 Alpaca Data) — ATH proximity gate ──────
        # Fetched once per pre-market window, cached in _main._spy_52w_high global.
        # Used by: Layer 5 dynamic MIN_SCORE (ATH proximity gate).
        # T1 source: Alpaca Data daily bars (252 bars = 1 trading year).
        # (globals accessed via _main)
        try:
            _spy_daily_yr = fetch_bars("SPY", config.TF_DAILY, num_bars=252)
            if _spy_daily_yr is not None and not _spy_daily_yr.empty:
                _main._spy_52w_high = float(_spy_daily_yr["close"].max())
                logger.info(
                    f"SPY 52w high refreshed: ${_main._spy_52w_high:.2f} (T1 Alpaca Data, "
                    f"{len(_spy_daily_yr)} bars)"
                )
                _save_spy_52w_high(_main._spy_52w_high)
        except Exception as _ath_err:
            logger.warning(f"SPY 52w high refresh failed: {_ath_err}")
            _ath_cached = _load_spy_52w_high()
            if _ath_cached is not None:
                _main._spy_52w_high = _ath_cached["value"]
                logger.warning(
                    "SPY 52w high: using cached value $%.2f (%d day(s) old) — "
                    "ATH protective floor/scalar remain active.",
                    _ath_cached["value"], _ath_cached["age_days"],
                )
            else:
                # True bootstrap: no cache exists AND today's fetch failed. Default
                # CONSERVATIVE (assume near-ATH) rather than fail-open — board mandate.
                try:
                    _spy_recent = fetch_bars("SPY", config.TF_DAILY, num_bars=2)
                    if _spy_recent is not None and not _spy_recent.empty:
                        _boot_close = float(_spy_recent["close"].iloc[-1])
                        _main._spy_52w_high = round(_boot_close * 1.005, 2)
                        logger.warning(
                            "SPY 52w high: no cache + fetch failed — bootstrap "
                            "conservative $%.2f (0.5%% above last close $%.2f), "
                            "ATH protection ACTIVE this session.",
                            _main._spy_52w_high, _boot_close,
                        )
                except Exception as _boot_err:
                    logger.error(
                        "SPY 52w high: no cache, fetch failed, bootstrap fallback "
                        "ALSO failed (%s) — ATH gate inactive this session "
                        "(self-heals next pre-market).", _boot_err,
                    )

        # ── Per-symbol 52w high cache — ATH proximity gate (Bug 3 fix) ───────────
        # Bug 3: the ATH gate was comparing every symbol against SPY's 52w high.
        # Fix: cache each watchlist symbol's own 252-day close max pre-market.
        # _main.check_partial_exits() uses _main._sym_52w_high_cache.get(symbol, 0) at entry time.
        # Only built once per session (populated = already fresh; cleared on restart).
        if not _main._sym_52w_high_cache:
            _ath_watchlist = list(config.WATCHLIST) + list(getattr(config, "LEVERAGED_TICKERS", []))
            _ath_hit = 0
            for _ath_sym in _ath_watchlist:
                try:
                    _ath_df = fetch_bars(_ath_sym, config.TF_DAILY, num_bars=252)
                    if _ath_df is not None and not _ath_df.empty:
                        _main._sym_52w_high_cache[_ath_sym] = float(_ath_df["close"].max())
                        _ath_hit += 1
                except Exception as _ath_sym_err:
                    _main._sym_52w_high_cache[_ath_sym] = float("inf")
                    logger.warning(
                        "[%s] 52w high cache miss — ATH gate disabled (inf sentinel): %s",
                        _ath_sym, _ath_sym_err,
                    )
            logger.info(
                f"Per-symbol 52w high cache built: {_ath_hit}/{len(_ath_watchlist)} symbols "
                f"(T1 Alpaca Data)"
            )

        try:
            from data.fmp_client import preload_earnings_week
            _watchlist_syms = list(config.WATCHLIST) + list(getattr(config, "LEVERAGED_TICKERS", []))
            preload_earnings_week(_watchlist_syms)
        except Exception as _ew_err:
            logger.warning(f"Pre-market earnings batch failed: {_ew_err}")

        try:
            from data.fmp_client import get_analyst_sentiment
            _watchlist_syms = list(config.WATCHLIST)
            _main._analyst_sentiment = {}
            _as_failed = []
            for _wsym in _watchlist_syms:
                # M-14: per-symbol try/except — one FMP failure must not abort the whole batch.
                # Prior outer-only try/except left all subsequent symbols as NEUTRAL when any failed.
                try:
                    _main._analyst_sentiment[_wsym] = get_analyst_sentiment(_wsym)
                except Exception as _sym_as_err:
                    _as_failed.append(_wsym)
                    logger.warning(
                        "[%s] analyst sentiment fetch failed — defaulting to NEUTRAL: %s",
                        _wsym, _sym_as_err,
                    )
            if _as_failed:
                logger.warning(f"Analyst sentiment: {len(_as_failed)} symbol(s) failed FMP fetch — using NEUTRAL for {_as_failed}")
            _bearish_syms = [s for s, v in _main._analyst_sentiment.items() if v.get("signal") == "BEARISH"]
            _bullish_syms = [s for s, v in _main._analyst_sentiment.items() if v.get("signal") == "BULLISH"]
            if _bearish_syms or _bullish_syms:
                logger.info(
                    f"Analyst sentiment: BULLISH={_bullish_syms} BEARISH={_bearish_syms}"
                )
        except Exception as _as_err:
            logger.warning(f"Pre-market analyst sentiment refresh failed: {_as_err}")

        try:
            from scan_to_html import write_scan_html
            write_scan_html(portfolio_value=get_portfolio_value(),
                            open_trades=dict(tracker.open_trades),
                            spy_event_type=_main._spy_event_type,
                            daily_pnl=getattr(risk, "equity_pnl", None) or 0.0,   # P1-PNL-AUDIT: equity delta matches dashboard formula
                            tqi_history=list(kelly._tqi_history),
                            confirm_gate=gate_state.entry_confirm_buffer)
        except (Exception, SystemExit) as _e:
            logger.warning(f"Pre-market HTML write failed: {_e}")
        _touch_cycle_ts()
        return

    # ── Market hours — still update scanner HTML with latest prices overnight ──
    # MN-AH-GATE: time-based fallback if API fails — ensures _main.check_exits() still runs during RTH (GAI audit 2026-04-29)
    try:
        _rcy_market_open = is_market_open()
    except Exception as _e_rcy_clock:
        _et_now = datetime.now(ET)
        _mins_et = _et_now.hour * 60 + _et_now.minute
        _rcy_market_open = (_et_now.weekday() < 5) and (570 <= _mins_et < 960)
        logger.warning(
            f"run_cycle: is_market_open() failed — time-based fallback: "
            f"market={'open' if _rcy_market_open else 'closed'}: {_e_rcy_clock}"
        )
    if not _rcy_market_open:
        logger.info("Market closed — updating scan prices, no entries.")
        try:
            from scan_to_html import write_scan_html
            write_scan_html(
                portfolio_value=portfolio_value,
                open_trades=dict(tracker.open_trades),
                spy_event_type=_main._spy_event_type,
                daily_pnl=getattr(risk, "equity_pnl", None) or 0.0,   # P1-PNL-AUDIT: equity delta matches dashboard formula
                tqi_history=list(kelly._tqi_history),
                confirm_gate=gate_state.entry_confirm_buffer,
            )
        except (Exception, SystemExit) as _e:
            logger.warning(f"Overnight scan HTML write failed: {_e}")
        # Write bot_status.json overnight — includes live news scan so alerts
        # hit the dashboard feed the moment they happen, not just during RTH.
        try:
            news.scan_breaking_news()
            _news_summary = news.get_summary() if news else {}
            _today_str = datetime.now(ET).strftime("%Y-%m-%d")
            _err_count = 0; _warn_count = 0; _err_samples: list[str] = []
            _NOISE = ("Finnhub", "TruthSocial", "Truth Social", "news_monitor", "Shorting:",
                      "too many requests", "not allowed to short", "position size < 1",
                      "already has Alpaca")
            _log_path = str(_PROJECT_ROOT / "logs" / "mtf_bot.log")  # RC-2 fix
            with open(_log_path) as _lf:
                for _line in _lf:
                    if _today_str not in _line[:20]: continue
                    if " ERROR " in _line:
                        _err_count += 1
                        if len(_err_samples) < 5:
                            _msg_start = _line.find(" ERROR ")
                            _err_samples.append(_line[_msg_start+7:].strip()[:120])
                    elif " WARNING " in _line and not any(x in _line for x in _NOISE):
                        _warn_count += 1
            _s = {"timestamp":      datetime.now(ET).isoformat(),
                  "spy_event_type": _main._spy_event_type,   # P0-REGIME: dashboard fallback when hybrid_state.json is absent
                  "news_summary":   _news_summary,
                  "news_items":     _news_summary.get("news_items", []),
                  "errors_today":   _err_count,
                  "warnings_today": _warn_count,
                  "error_samples":  _err_samples}
            from state.persistence import write_bot_status
            write_bot_status(_s)
        except Exception as _bse:
            logger.warning(f"bot_status.json write failed — dashboard feed may be stale: {_bse}")
        # (globals accessed via _main)
        _today_eod = datetime.now(ZoneInfo("America/Los_Angeles")).date()
        if _main._last_eod_summary_date != _today_eod:
            _ah_reconciled = False
            try:
                _ah_eod_path = _PROJECT_ROOT / "logs" / f"eod_{_today_eod}.json"
                if _ah_eod_path.exists():
                    _ah_reconciled = bool(json.loads(_ah_eod_path.read_text()).get("_reconcile_ts"))
            except Exception as _ahce:
                logger.warning("AH EOD sentinel check failed — assuming unreconciled: %s", _ahce)
            if _ah_reconciled:
                _main._last_eod_summary_date = _today_eod  # type: ignore[assignment]
                logger.info("AH EOD: _reconcile_ts present — preserving Alpaca-corrected P&L.")
            else:
                try:
                    tracker.write_eod_summary(kelly_sizer=kelly)
                    _main._last_eod_summary_date = _today_eod  # type: ignore[assignment]
                except Exception as _eode:
                    logger.warning(f"AH write_eod_summary failed: {_eode}")
        else:
            logger.debug("AH EOD summary already written today — skipping repeat run.")
        # ── U-2: Spawn weekly_review.html — AH cycle (post-close path) ───────
        # The RTH spawn at line ~1711 is unreachable after market close because
        # the closed-market early-return fires before reaching it. This block
        # runs every AH cycle; _main._last_weekly_review_spawn_date deduplicates so
        # only one spawn fires per calendar day regardless of which path wins.
        try:
            import subprocess as _sp_ah
            from zoneinfo import ZoneInfo as _ZI_ah
            # (globals accessed via _main)
            _pdt_ah       = _ZI_ah("America/Los_Angeles")
            _now_pdt_ah   = datetime.now(_pdt_ah)
            _today_pst_ah = _now_pdt_ah.date()
            # Spawn window: after 1:15 PM PST (= 4:15 PM ET) so EOD writes complete first
            _past_window_ah = (
                (_now_pdt_ah.hour > 13) or
                (_now_pdt_ah.hour == 13 and _now_pdt_ah.minute >= 15)
            )
            # Allow re-spawn if today's EOD file was written after our last spawn
            _eod_path_ah  = str(_PROJECT_ROOT / "logs" / f"eod_{_today_pst_ah}.json")  # RC-2 fix
            _eod_newer_ah = False
            try:
                import time as _wr_t_ah
                if os.path.exists(_eod_path_ah):
                    _eod_mtime_ah  = os.path.getmtime(_eod_path_ah)
                    _spawn_ts_ah   = getattr(_wr_t_ah, "_last_wr_spawn_ts", 0)
                    _eod_newer_ah  = _eod_mtime_ah > _spawn_ts_ah
            except Exception as _mte:
                logger.info("AH spawn EOD mtime check failed: %s", _mte)
            if _past_window_ah and (
                _main._last_weekly_review_spawn_date != _today_pst_ah or _eod_newer_ah
            ):
                _is_friday_ah = (_now_pdt_ah.weekday() == 4)
                _cmd_ah = [
                    sys.executable,
                    str(_PROJECT_ROOT / "weekly_review.py"),  # RC-2 fix
                ]
                if _is_friday_ah:
                    _cmd_ah.append("--analyze")
                _sp_ah.Popen(_cmd_ah, stdout=_sp_ah.DEVNULL, stderr=_sp_ah.DEVNULL)
                _main._last_weekly_review_spawn_date = _today_pst_ah
                import time as _wr_t2_ah
                _wr_t2_ah._last_wr_spawn_ts = _wr_t2_ah.time()  # type: ignore[attr-defined]
                logger.info(f"[AH] weekly_review.py spawned for {_today_pst_ah}")
        except Exception as _wre:
            logger.warning(f"AH weekly_review.py spawn failed: {_wre}")
        # ── Overnight tagging — runs every closed-market cycle ───────────────
        # Tags any open position as overnight when:
        #   (a) entry was on a prior calendar day, OR
        #   (b) entry was today AND market has closed (≥4:00 PM ET) — same-day
        #       entry held past close that the EOD block missed due to timing race.
        # Idempotent: safe to run every cycle.
        _now_tag      = datetime.now(ET)
        _today_date   = _now_tag.strftime("%Y-%m-%d")
        _mins_tag     = _now_tag.hour * 60 + _now_tag.minute
        _past_close   = _mins_tag >= (16 * 60)   # 4:00 PM ET or later
        for _sym, _tr in tracker.open_trades.items():
            _entry_date  = _tr.get("entry_time", "")[:10]
            _prior_day   = _entry_date < _today_date
            _same_day_ah = _past_close and _entry_date == _today_date
            if not _tr.get("overnight") and (_prior_day or _same_day_ah):
                _tr["overnight"]       = True
                _tr["overnight_since"] = _tr.get("overnight_since") or _now_tag.isoformat()
                logger.info(
                    f"[{_sym}] Tagged overnight "
                    f"({'prior day' if _prior_day else 'same-day held past close'})"
                )
                tracker._save_log()
        # ── AH GTC stop submission — 4:05–10:00 PM ET ───────────────────────
        # Decoupled from the EOD block which races the 4:00 PM market close and
        # fires AFTER the closed-market early return (never reached at 4:00 PM).
        # This loop runs every closed-market cycle; the gtc_stop_order_id gate
        # ensures submission happens exactly once per overnight position.
        # Window: 4:05–10:00 PM ET (extended past 8 PM for late-day bot restarts).
        _now_ah   = datetime.now(ET)
        _mins_ah  = _now_ah.hour * 60 + _now_ah.minute
        _is_ah    = (16 * 60 + 5) <= _mins_ah < (22 * 60)  # 4:05–10:00 PM ET
        if _is_ah and tracker.open_trades:
            import random as _rnd_ah
            for _gsym, _gtr in list(tracker.open_trades.items()):
                if not _gtr.get("overnight"):
                    continue
                if _gtr.get("gtc_stop_order_id"):
                    continue  # already submitted this session
                _gdir        = _gtr["direction"]
                _gentry      = _gtr.get("entry_price", 0)
                _gatr_stop   = _gtr.get("trail_stop") or _gtr.get("stop")
                _gstop_price = _gatr_stop  # default: ATR-based stop
                # C-1: Guard — cannot compute 0.5R threshold without a valid ATR stop.
                # If ATR stop is missing, submitting any stop is unsafe — skip entirely.
                if not _gatr_stop:
                    logger.warning(
                        f"[{_gsym}] AH GTC: ATR stop missing — cannot compute 0.5R "
                        f"threshold. Skipping GTC to avoid bad stop placement."
                    )
                    continue
                try:
                    _gdf = fetch_bars(_gsym, config.TF_15M, num_bars=2)
                    if not _gdf.empty:
                        _gcur = float(_gdf["close"].iloc[-1])
                        _gprofitable = (
                            (_gdir == "short" and _gcur < _gentry) or
                            (_gdir == "long"  and _gcur > _gentry)
                        )
                        # C-1: Require profit >= 0.5R before moving stop to breakeven.
                        # Prevents penny-stop bug where any float > 0 triggered breakeven.
                        _grisk_dist   = abs(_gentry - _gatr_stop)
                        _gprofit_dist = abs(_gcur - _gentry)
                        _half_r       = _grisk_dist * 0.5
                        if _gprofitable and _grisk_dist > 0 and _gprofit_dist >= _half_r:
                            _gstop_price = _gentry
                            logger.info(
                                f"[{_gsym}] AH GTC: profitable (${_gcur:.2f} vs entry "
                                f"${_gentry:.2f}), profit ${_gprofit_dist:.2f} >= 0.5R "
                                f"${_half_r:.2f} — stop at BREAKEVEN."
                            )
                        else:
                            logger.info(
                                f"[{_gsym}] AH GTC: profit dist ${_gprofit_dist:.2f} < "
                                f"0.5R ${_half_r:.2f} or at loss (${_gcur:.2f} vs entry "
                                f"${_gentry:.2f}) — stop at ATR level ${_gatr_stop:.2f}."
                            )
                except Exception as _gpe:
                    logger.warning(f"[{_gsym}] AH GTC: price fetch failed — using ATR stop. {_gpe}")
                # VIX-adjusted overnight GTC stop widening — mirrors risk_manager.py RTH logic.
                # Only widens when stop is at a loss vs entry; skips breakeven/profit stops.
                _gstop_at_be     = (_gstop_price == _gentry)
                _gstop_in_profit = ((_gdir == "long"  and _gstop_price > _gentry) or
                                    (_gdir == "short" and _gstop_price < _gentry))
                if not _gstop_at_be and not _gstop_in_profit:
                    try:
                        import yfinance as _yf_gtc  # type: ignore[import-untyped]
                        _vix_gtc = float(
                            _yf_gtc.Ticker("^VIX").fast_info.get("lastPrice") or
                            _yf_gtc.Ticker("^VIX").history(period="1d")["Close"].iloc[-1]
                        )
                        # AWP audit fix (2026-06-28): this block's own comment
                        # claims parity with risk_manager.py's RTH VIX-widening
                        # logic, but risk_manager.py switched from this exact
                        # discrete step function to a continuous curve on
                        # 2026-06-24 (commit 7e5c983, board 4-0 + Gro + GAI) —
                        # this AH mirror was never updated, so the parity claim
                        # was false (e.g. VIX=27 got 1.5x here vs ~1.7x RTH).
                        # Restored to the same formula. Board (BoD tie-break
                        # 4-0 after a 2-2 domain split) + Gro + GAI all
                        # independently confirmed this as a genuine bug
                        # requiring a fix, not an intentional design choice.
                        _vix_mult_gtc = min(
                            1.0 + max(0.0, _vix_gtc - 20.0) * 0.1, 2.0
                        )
                        if _vix_mult_gtc > 1.0:
                            _gdist_gtc   = abs(_gentry - _gstop_price)
                            _gstop_price = (
                                round(_gentry - _gdist_gtc * _vix_mult_gtc, 2) if _gdir == "long"
                                else round(_gentry + _gdist_gtc * _vix_mult_gtc, 2)
                            )
                            logger.info(
                                f"[{_gsym}] AH GTC: VIX={_vix_gtc:.1f} → stop widened "
                                f"{_vix_mult_gtc:.1f}x to ${_gstop_price:.2f}"
                            )
                    except Exception as _vix_gtc_err:
                        logger.warning(
                            f"[{_gsym}] AH GTC: VIX fetch failed — ATR stop unchanged. "
                            f"{_vix_gtc_err}"
                        )
                # Fix B (Apr 14 2026): Verify Alpaca holds this position before submitting
                # the AH GTC stop.  If the position was closed during RTH but the tracker
                # wasn't updated in-cycle, the AH loop would fire a phantom stop order
                # (rejected by Alpaca) and generate a false-alarm Slack alert.
                try:
                    _galpaca_pos = get_open_position(_gsym)
                except Exception as _gap_err:
                    _galpaca_pos = True  # fail-open: assume position exists if check errors
                    logger.warning(
                        f"[{_gsym}] AH GTC: Alpaca position check failed ({_gap_err}). "
                        f"Proceeding with GTC stop submission."
                    )
                if _galpaca_pos is None:
                    logger.warning(
                        f"[{_gsym}] AH GTC: position NOT in Alpaca — closing phantom "
                        f"tracker record instead of submitting GTC stop."
                    )
                    _gah_exit_px = _fetch_actual_fill_price(_gsym, _gtr, poll_secs=0)
                    _gah_pnl = tracker.record_exit(_gsym, _gah_exit_px,
                                                    reason="external_close_detected_ah",
                                                    mri_level=mri.level() if mri else "NORMAL",
                                                    # Verified: reached only under
                                                    # `if _galpaca_pos is None` above.
                                                    alpaca_confirmed_absent=True)
                    if risk is not None:
                        risk.register_close(_gah_pnl or 0.0)
                    continue
                if _gstop_price:
                    _goffset = round(_rnd_ah.uniform(0.01, 0.05), 2)
                    if _gdir == "short":
                        _gstop_price = round(_gstop_price + _goffset, 2)
                    else:
                        _gstop_price = round(_gstop_price - _goffset, 2)
                    _gside   = "sell" if _gdir == "long" else "buy"
                    _gqty    = abs(int(_gtr.get("qty_remaining", _gtr["qty"])))
                    # COVER-ON-BREACH pre-flight (board + Gro + GAI unanimous, Rafael
                    # ratified 2026-07-01): if the protective stop is already breached
                    # (short buy-stop <= market / long sell-stop >= market) Alpaca rejects
                    # it (42210000) and the position goes NAKED overnight. Cover now via a
                    # marketable extended-hours limit (fills 4-8 PM ET); the fill is recorded
                    # by cancel_and_reconcile_gtc_stops at next startup (it keys on order
                    # status 'filled', not order type — verified). If the extended-hours cover
                    # can't be placed (after 8 PM, no session), the RTH cover-on-breach in
                    # gtc_manager.submit_rth_day_stops closes it at the 9:30 open.
                    _gmkt = None
                    try:
                        if _galpaca_pos not in (None, True):
                            _gmkt = float(getattr(_galpaca_pos, "current_price", 0)) or None
                    except (TypeError, ValueError):
                        _gmkt = None
                    if _gmkt is not None and (
                        (_gdir == "short" and _gstop_price <= _gmkt) or
                        (_gdir == "long"  and _gstop_price >= _gmkt)
                    ):
                        _gcov_side  = "buy" if _gdir == "short" else "sell"
                        _gcov_limit = round(_gmkt * (1.01 if _gdir == "short" else 0.99), 2)
                        _gatr_n = _gtr.get("atr_value") or 0
                        if _gatr_n > 0 and abs(_gmkt - _gstop_price) > 3 * _gatr_n:
                            logger.critical(
                                f"[{_gsym}] STOP BREACH >3xATR past stop (market ${_gmkt:.2f} "
                                f"vs stop ${_gstop_price:.2f}, ATR ${_gatr_n:.2f}) — sizing review flagged."
                            )
                        logger.critical(
                            f"[{_gsym}] STOP BREACHED overnight ({_gdir}): stop ${_gstop_price:.2f} "
                            f"{'<=' if _gdir == 'short' else '>='} market ${_gmkt:.2f} — no valid stop "
                            f"placeable; covering now (extended-hours marketable limit)."
                        )
                        _gcov_ord = submit_limit_order(
                            _gsym, _gqty, _gcov_side, _gcov_limit, extended_hours=True
                        )
                        if _gcov_ord:
                            tracker.set_gtc_stop_order_id(_gsym, str(getattr(_gcov_ord, "id", "")))
                            logger.critical(
                                f"[{_gsym}] Cover-on-breach limit submitted: {_gcov_side.upper()} "
                                f"{_gqty} @ ${_gcov_limit:.2f} | order {getattr(_gcov_ord, 'id', '?')} "
                                f"(exit recorded at next reconcile on fill)."
                            )
                            try:
                                send_slack(
                                    f":rotating_light: [{_gsym}] stop breached overnight — covering "
                                    f"({_gcov_side} {_gqty} @ ${_gcov_limit:.2f}). Verify fill."
                                )
                            except Exception as _cse:
                                logger.debug("[%s] cover Slack alert failed — %s", _gsym, _cse)
                        elif not _gtr.get("_breach_cover_alerted"):
                            _gtr["_breach_cover_alerted"] = True
                            tracker._save_log()
                            logger.critical(
                                f"[{_gsym}] STOP BREACHED overnight but extended-hours cover could "
                                f"not be placed (likely after 8 PM ET). Position naked until 9:30 open, "
                                f"where the RTH cover-on-breach will close it. Cover manually if urgent."
                            )
                            try:
                                send_slack(
                                    f":rotating_light: [{_gsym}] stop breached overnight; AH cover "
                                    f"unavailable — will cover at 9:30 open (RTH breach-cover). "
                                    f"Cover manually if urgent."
                                )
                            except Exception as _cse:
                                logger.debug("[%s] cover-defer Slack alert failed — %s", _gsym, _cse)
                        continue
                    logger.info(
                        f"[{_gsym}] AH GTC stop submitting: {_gside.upper()} "
                        f"{_gqty} @ ${_gstop_price:.2f} (offset ${_goffset:.2f})"
                    )
                    _gorder = submit_gtc_stop_order(
                        symbol=_gsym, qty=_gqty,
                        side=_gside, stop_price=_gstop_price,
                    )
                    if _gorder:
                        tracker.set_gtc_stop_order_id(_gsym, str(_gorder.id))  # type: ignore[attr-defined]
                        logger.info(f"[{_gsym}] AH GTC stop submitted: order {_gorder.id}")  # type: ignore[attr-defined]
                        # H-3: Confirm order not silently rejected after initial acceptance.
                        # Poll once after 1 second — only flag terminal statuses (rejected/cancelled).
                        # pending_new is normal and does not indicate failure.
                        try:
                            time.sleep(1.0)
                            _gconfirm = get_order(str(_gorder.id))  # type: ignore[attr-defined]
                            # H-3 fix: explicit None guard — order not found ≠ confirmed active
                            if _gconfirm is None:
                                logger.warning(
                                    f"[{_gsym}] AH GTC stop confirm: order not found "
                                    f"after 1s — state unknown. Will reconcile at next startup."
                                )
                            else:
                                _gstatus = str(getattr(_gconfirm, "status", "unknown")).lower()
                                # H-3 fix: include filled/partially_filled — stop triggered
                                # immediately means price was already at/through stop level.
                                # Do NOT clear ID or record exit here — let
                                # _cancel_and_reconcile_gtc_stops handle it at next startup
                                # so the exit is recorded correctly via the normal path.
                                if _gstatus in ("rejected", "cancelled", "expired"):
                                    tracker.open_trades[_gsym]["gtc_stop_order_id"] = None
                                    tracker._save_log()
                                    logger.warning(
                                        f"[{_gsym}] AH GTC stop REJECTED post-submit "
                                        f"(status={_gstatus}) — gtc_stop_order_id cleared. "
                                        f"NO exchange-level stop. Check Alpaca manually."
                                    )
                                elif _gstatus in ("filled", "partially_filled"):
                                    logger.warning(
                                        f"[{_gsym}] AH GTC stop FILLED immediately after submit "
                                        f"(status={_gstatus}) — stop may have been too close to "
                                        f"current price. Reconciliation will record exit at next startup."
                                    )
                                else:
                                    logger.info(
                                        f"[{_gsym}] AH GTC stop active: status={_gstatus}"
                                    )
                        except Exception as _gce:
                            logger.warning(
                                f"[{_gsym}] AH GTC stop confirm poll failed: {_gce}"
                            )
                    else:
                        # C-2: Clear stale order ID — position has NO exchange-level stop
                        # Without this, the bot reads the old ID next cycle and assumes
                        # protection that no longer exists (phantom protection bug).
                        tracker.open_trades[_gsym]["gtc_stop_order_id"] = None
                        tracker._save_log()
                        # Escalate only if the AH window is closing (after 9 PM ET).
                        # Before 9 PM: GTC failed — the bot will attempt again next cycle.
                        # After 9 PM: no more retries tonight, manual action needed.
                        _ah_retry_msg = (
                            "AH window closing — MANUAL STOP REQUIRED in Alpaca."
                            if _mins_ah >= (21 * 60)
                            else f"GTC failed — will retry next cycle. Internal stop=${_gstop_price:.2f} active."
                        )
                        logger.warning(
                            f"[{_gsym}] AH GTC stop attempt failed — gtc_stop_order_id cleared. "
                            f"{_ah_retry_msg}"
                        )
            tracker._save_log()
        # 24/7 position exit monitoring — partial exits, trail stops, breakeven
        try:
            _main._check_exits_extended_hours(tracker, risk, kelly)
        except Exception as _ehe:
            logger.warning(f"EH exit check failed: {_ehe}")
        # Overnight entry check — Phase 1 dry-run (8 PM – midnight ET only)
        try:
            _main._overnight_entry_check(tracker, risk, kelly, calendar)
        except Exception as _oe:
            logger.warning(f"Overnight entry check failed: {_oe}")
        _touch_cycle_ts()
        return

    if _main._too_early():
        logger.info("Before 9:30 AM ET — waiting for market to open.")
        _touch_cycle_ts()
        return

    # ── RTH session start: submit DAY stops for overnight positions missing exchange-level stop ──
    # Fires once per calendar date. DAY orders expire at 4 PM ET —
    # no conflict with tonight's AH GTC submission.
    _main._submit_rth_day_stops(tracker, risk)

    # ── ORB Gate: compute SPY 15-min opening range at 9:55 AM ET ─────────────────
    # Board vote 2026-05-17, 26/26 unanimous. Fail-closed: any feed error → BLOCK_ALL.
    # DS/GAI audit P1: require _orb_high > _orb_low AND _orb_low > 0.0 to reject flat/halted opens.
    if config.ORB_ENABLED:
        _now_orb      = datetime.now(ET)
        _mins_orb     = _now_orb.hour * 60 + _now_orb.minute
        _orb_date_str = _now_orb.strftime("%Y-%m-%d")
        if _mins_orb >= config.ORB_COMPUTE_AFTER_MIN and _main._orb_computed_date != _orb_date_str:
            try:
                _orb_raw = fetch_bars("SPY", config.TF_5M, num_bars=100)
            except Exception as _orb_ex:
                logger.warning(f"ORB: fetch_bars failed — BLOCK_ALL entries today (fail-closed): {_orb_ex}")
                _main._orb_feed_failed   = True
                _main._orb_computed_date = _orb_date_str
                _orb_raw = None
            if _orb_raw is not None:
                if len(_orb_raw) == 0:
                    logger.warning("ORB: empty bar response — BLOCK_ALL entries today (fail-closed)")
                    _main._orb_feed_failed   = True
                    _main._orb_computed_date = _orb_date_str
                else:
                    _idx_et      = _orb_raw.index.tz_convert(ET)
                    _bar_mins_et = _idx_et.hour * 60 + _idx_et.minute
                    _orb_start   = 9 * 60 + 30
                    _orb_end     = _orb_start + config.ORB_WINDOW_MINUTES
                    _orb_window  = _orb_raw[
                        (_idx_et.strftime("%Y-%m-%d") == _orb_date_str) &
                        (_bar_mins_et >= _orb_start) &
                        (_bar_mins_et < _orb_end)
                    ]
                    if len(_orb_window) < 3:
                        logger.warning(
                            f"ORB: only {len(_orb_window)}/3 bars in 9:30–9:44 ET window "
                            f"— BLOCK_ALL entries today (fail-closed)"
                        )
                        _main._orb_feed_failed = True
                    else:
                        _h = float(_orb_window["high"].max())
                        _l = float(_orb_window["low"].min())
                        if _h > _l and _l > 0.0:
                            _main._orb_high        = _h
                            _main._orb_low         = _l
                            _main._orb_feed_failed = False
                            logger.info(
                                f"ORB computed: HIGH={_h:.2f} LOW={_l:.2f} "
                                f"from {len(_orb_window)} bar(s) (9:30–9:44 ET)"
                            )
                        else:
                            logger.warning(
                                f"ORB: invalid range HIGH={_h} LOW={_l} "
                                f"(flat/zero/halted bars) — BLOCK_ALL entries today (fail-closed)"
                            )
                            _main._orb_feed_failed = True
                    _main._orb_computed_date = _orb_date_str

    if tod_phase == "opening":
        # 30-min opening buffer active (9:30–10:00 ET) — exits monitored, no new entries.
        # Rule 2 gap-down gate in _main.execute_entries() is unreachable (defence-in-depth only).
        # P5-H4: stale "buffer=0" comment removed; config.py:259 sets buffer=30.
        logger.info("Opening noise window active — monitoring exits only.")
        # AB-6R: refresh _main._last_vix before _main.check_partial_exits() so T2 stressed
        # threshold uses current VIX, not last cycle's stale value
        try:
            _opening_regime = regime.get_regime()
            _main._last_vix = float(_opening_regime.get("realized_vol", 0) or 0.0)
        except Exception as exc:
            logger.warning(
                "VIX regime refresh failed (using stale cached VIX=%.2f): %s",
                _main._last_vix, exc,
            )
        _main.check_partial_exits(tracker, kelly, risk, mri, last_vix=_main._last_vix)
        _main.check_exits(tracker, risk, mri_level=mri.level() if mri else "NORMAL",
                    kelly=kelly, last_vix=_main._last_vix, gate_state=gate_state)
        _touch_cycle_ts()
        return

    # ── EOD — hold overnight, submit GTC stops, flag positions ──────────────────────────────────────
    # Bot holds all positions overnight. Strategy is swing trading — no EOD flatten.
    # At market close: tag positions overnight + submit GTC stop to Alpaca for overnight protection.
    # GTC stops are cancelled at next market open so they don't appear on L2 during RTH.
    if trade_mode == config.TradeMode.INTRADAY and _main._should_flatten_eod():
        open_positions = list(tracker.open_trades.keys())
        if open_positions:
            logger.info(
                f"Market close — holding {len(open_positions)} position(s) overnight: "
                f"{open_positions}"
            )
            for symbol in open_positions:
                trade = tracker.open_trades[symbol]
                trade["overnight"]       = True
                trade["overnight_since"] = datetime.now(ET).isoformat()
                # GTC stop is submitted in the AH cycle (4:05–10:00 PM ET loop above)
                # to avoid the timing race at 4:00 PM market close.
            tracker._save_log()
        try:
            tracker.write_eod_summary(kelly_sizer=kelly)
        except Exception as _e:
            logger.warning(f"EOD summary write failed: {_e}")
        _touch_cycle_ts()
        return

    # ── Overnight pending order reconciliation (RTH) ─────────────────────────
    # Promote filled overnight limits → active; cancel unfilled after 9:45 AM.
    _pending = tracker.get_pending_overnight_entries()
    if _pending:
        _main._reconcile_pending_overnight_orders(tracker, risk)
        _still_pending = tracker.get_pending_overnight_entries()
        if _still_pending:
            _now_mins = now.hour * 60 + now.minute
            if _now_mins >= _main._OVERNIGHT_CANCEL_CUTOFF:
                for _psym, _ptrade in list(_still_pending):
                    _pid = _ptrade.get("order_id")
                    if _pid:
                        logger.info(
                            f"[{_psym}] 9:45 AM cutoff — cancelling unfilled overnight limit {_pid}"
                        )
                        cancel_order(_pid)
                    tracker.cancel_pending_entry(_psym)

    # ── Volatility regime ───────────────────────────────────────────────────────────────────────────
    regime_data = regime.get_regime()
    regime_size_mult = regime_data["size_mult"]
    if regime_data["regime"] != "normal":
        logger.info(f"Regime: {regime_data['note']}")

    # AB-6R: cache latest VIX so _main.check_partial_exits() can read it without
    # an extra fetch.  Updated every run_cycle() — stale by at most one scan.
    _main._last_vix = float(regime_data.get("realized_vol", 0) or 0.0)

    # ── Event risk check ───────────────────────────────────────────────────────────────────────────
    day_risk = calendar.get_day_risk()
    if not calendar.is_tradeable():
        logger.warning(f"BLACKOUT day — no trading: {day_risk.get('note', '')}")
        _touch_cycle_ts()
        return

    risk_note = day_risk.get("note", "")
    # Use calendar's authoritative size multiplier — single source of truth.
    # Pass now so post-release normalization can activate (HIGH_RISK 0.50x → 0.75x).
    event_size_mult = calendar.get_size_multiplier(now_et=now)
    if event_size_mult == 0.75 and day_risk.get("risk") and \
            day_risk["risk"].value == "high_risk":
        logger.info(
            f"Post-release normalization active ({risk_note}) — "
            f"sizing stepped up to 75% (was 50%)"
        )
    elif event_size_mult < 1.0:
        logger.warning(
            f"Risk day ({risk_note}) — sizing at {event_size_mult:.0%}"
        )

    # ── TOD size multiplier ───────────────────────────────────────────────────────────────────────────
    tod_size_mult = _tod_size_multiplier(tod_phase)
    if tod_phase == "midday":
        logger.info(f"Mid-day doldrums — sizing at {tod_size_mult:.0%}")

    # ── News scan — passive surveillance only ─────────────────────────────────
    # Fetches all 14 sources (Truth Social, Finnhub, GNews, Reuters/AP, War/Geo, Oil, etc.).
    # Logs and surfaces CAUTION/MONITOR items to dashboard. ZERO direct size impact.
    # Sizing is driven by SPY 5-min movement (primary gate) + MRI background score.
    try:
        news.scan_breaking_news()
    except Exception as _news_err:
        logger.error(f"[run_cycle] news.scan_breaking_news() failed: {_news_err}", exc_info=True)
    news_size_mult = news.get_news_size_multiplier()   # returns 1.0 always, 0.0 on HALT

    # ── MRI refresh — cross-asset background stress score ─────────────────────
    # Fetches TLT/HYG/LQD/USO/GLD/EWJ/EWG via fetch_bars (Alpaca Data T1).
    # yfinance is used only as a T4 fallback: ^VIX3M (not on Alpaca/FMP free
    # tier) always, plus VIX/JPY when FMP fails. See macro_risk_index.py.
    # Score 0–100 → size floor + MIN_SCORE floor applied below (market-reaction-first).
    # Does NOT gate entries — SPY 5-min trigger is still required.
    mri.refresh()
    # Bug 2 fix: inject news_monitor macro alert count into MRI score each cycle.
    # mri._compute() is price-only; news alerts may lead prices by many minutes.
    # inject_news_state() adds a calibrated bonus when _macro_risk_active is True
    # so MRI level reflects both market prices AND confirmed macro news backdrop.
    _nm_summary = news.get_summary()
    # Pass price_score (pre-injection) so inject_news_state can gate the bonus
    # when all cross-asset price components are silent (market-reaction-first fix).
    mri.inject_news_state(
        _nm_summary.get("macro_risk_alert_count", 0),
        price_score=mri.price_score(),
    )
    _log_trade_event(
        "mri_refresh", symbol="MRI", price=0.0, size=0,
        mri_level=mri.level(), score=mri.score(),
        data_source="alpaca_data",
        components=mri.components(),
    )

    # Push news + MRI state to tracker for dashboard display
    _news_summary = _nm_summary
    _news_summary["mri"] = mri.summary()
    tracker.attach_news_summary(_news_summary)

    # ── HALT: news-keyword gate — INTERIM (2026-07-08, board+Gro+GAI, "disarm now") ──
    # A news-keyword HALT NO LONGER LIQUIDATES the book. Until 2026-07-08 a raw substring
    # match (KEYWORDS_HALT, e.g. "national emergency") set news_size_mult=0.0 →
    # _safe_close_all(circuit_breaker=True) → the ENTIRE non-QHM book was market-sold. It
    # fired on a benign tariff QUESTION ("Can Trump cut off all trade with Spain?") that
    # contained "national emergency" (~-$26, 6 positions, 11s). Interim: a keyword HALT now
    # BLOCKS NEW ENTRIES ONLY (see the _news_halt_block_entries return below, placed AFTER
    # exit management so existing positions are still managed by their GTC stops + the SPY
    # 5-min EXTREME engine). A real "close everything" reflex returns in Build F, triggered
    # by a real price circuit-breaker / exchange-halt signal — never a keyword.
    # _safe_close_all is UNCHANGED and still used by that future path. DELIBERATE: this
    # interim does NOT set the session _halt_entries_for_session flag — the block is
    # per-cycle and self-clears when the (false) keyword ages out of the news window,
    # instead of locking out entries until 4pm ET on a false positive.
    _news_halt_block_entries = False
    if news_size_mult == 0.0:
        logger.critical(
            "⛔ NEWS HALT (keyword) — BLOCKING NEW ENTRIES this cycle; positions "
            "RETAINED (managed by stops + SPY price engine). No liquidation "
            "(interim; Build F adds a price/exchange-triggered close reflex)."
        )
        _news_halt_block_entries = True

    # ── Hybrid Market Reaction Engine ─────────────────────────────────────────
    # Four-layer system combining Proposals A + B + C + D:
    #
    #  Layer 1 (A): SPY 5-min bar-over-bar trigger (>1% = active, >2% = extreme)
    #  Layer 2 (D): QQQ 5-min confirmation — both moving = BROAD, SPY only = SECTOR
    #  Layer 3 (B): News context after price trigger — WAR_GEO | FED_POLICY | TECHNICAL
    #  Layer 4 (C): Continuous size multiplier — scales with SPY magnitude, no cliff edges
    #
    # Persistence by event type: WAR_GEO=5 scans, FED/TECH=3 scans, SECTOR=2 scans
    # ─────────────────────────────────────────────────────────────────────────────
    # (globals accessed via _main: _spy_risk_active/direction/magnitude/scans_left)
    # Note: _main._spy_event_type declared global at top of run_cycle() (AB-1 header use)

    _spy_5m_pct  = 0.0
    _qqq_5m_pct  = 0.0
    _spy_rvol_5m = 1.0   # default full conviction — only downgraded on confirmed thin tape

    # (globals accessed via _main)
    try:
        # DATA-2e: T1 Alpaca Data API (replaces yfinance download for SPY/QQQ 5m gate).
        # Alpaca returns clean lowercase columns — no MultiIndex, no pandas import needed.
        # 100 bars covers a full RTH session (78 bars) with pre-market buffer.
        _spy_5m_df = fetch_bars("SPY", config.TF_5M, num_bars=100)
        _qqq_5m_df = fetch_bars("QQQ", config.TF_5M, num_bars=100)
        if _spy_5m_df is not None and len(_spy_5m_df) >= 2:
            _sc0 = float(_spy_5m_df["close"].values[-1])
            _main._spy_last_close = _sc0   # persist for ATH Layer 5
            _sc1 = float(_spy_5m_df["close"].values[-2])
            if _sc1 > 0:
                _spy_5m_pct = (_sc0 - _sc1) / _sc1 * 100
            # RVOL: current bar volume vs today's session average — filters thin-tape false positives
            _spy_5m_vol_now = float(_spy_5m_df["volume"].values[-1]) if "volume" in _spy_5m_df.columns else 0.0
            _spy_5m_vol_avg = float(_spy_5m_df["volume"].iloc[:-1].mean()) if "volume" in _spy_5m_df.columns and len(_spy_5m_df) > 1 else 0.0  # Build #10: exclude triggering bar from avg
            _spy_rvol_5m    = (_spy_5m_vol_now / _spy_5m_vol_avg)  if _spy_5m_vol_avg > 0 else 1.0
        if _qqq_5m_df is not None and len(_qqq_5m_df) >= 2:
            _qc0 = float(_qqq_5m_df["close"].values[-1])
            _qc1 = float(_qqq_5m_df["close"].values[-2])
            if _qc1 > 0:
                _qqq_5m_pct = (_qc0 - _qc1) / _qc1 * 100
        # Build #11: successful fetch — reset failure counter
        if _main._spy_fetch_failures > 0:
            logger.info(f"SPY/QQQ fetch recovered after {_main._spy_fetch_failures} failure(s) — resuming normal decay.")
        _main._spy_fetch_failures = 0
        if _main._spy_fetch_alert_sent:   # type: ignore[attr-defined]  # re-arm: next streak triggers a fresh alert
            _main._spy_fetch_alert_sent = False  # type: ignore[attr-defined]
    except Exception as _mkt5_err:
        # Build #11: track consecutive failures; suppress event decay when data is unavailable
        _main._spy_fetch_failures += 1
        # T1 fail-streak alert — fires once per streak at ≥3 consecutive failures.
        # Re-arms automatically after successful fetch; systemd restart = fresh process + fresh slate.
        if _main._spy_fetch_failures >= 3 and not _main._spy_fetch_alert_sent:  # type: ignore[attr-defined]
            _t1_pos = len(tracker.open_trades)
            _t1_msg = (
                f":satellite: *T1 FEED DOWN* — {_main._spy_fetch_failures} consecutive "
                f"SPY fetch failures (~{_main._spy_fetch_failures * 5} min). "
                f"Open positions: {_t1_pos}. Manual monitoring required."
            )
            logger.critical(
                "T1 fail-streak: %d consecutive SPY fetch failures", _main._spy_fetch_failures
            )
            try:
                send_slack(_t1_msg)
            except Exception as _t1_err:
                logger.warning("T1 alert send failed: %s", _t1_err)
            _main._spy_fetch_alert_sent = True  # type: ignore[attr-defined]
        if _main._spy_fetch_failures >= 3 and _main._spy_risk_active:
            logger.warning(
                f"⚠️  SPY/QQQ fetch FAILED {_main._spy_fetch_failures}x consecutively — "
                f"active {_main._spy_event_type} event held, decay suppressed until data recovers. "
                f"Error: {_mkt5_err}"
            )
        else:
            logger.warning(f"SPY/QQQ 5-min fetch failed ({_main._spy_fetch_failures}x) — using prior risk state: {_mkt5_err}")

    # Layer 1 + 2: Classify the bar
    _SPY_TRIGGER   = 1.0    # % in one 5-min bar → active
    _SPY_EXTREME   = 2.0    # % in one 5-min bar → block all entries
    _QQQ_CONFIRM   = 0.7    # QQQ threshold to confirm BROAD (slightly lower — QQQ more volatile)

    _spy_triggered = abs(_spy_5m_pct) >= _SPY_TRIGGER
    _spy_extreme   = abs(_spy_5m_pct) >= _SPY_EXTREME
    _qqq_confirmed = abs(_qqq_5m_pct) >= _QQQ_CONFIRM
    _same_dir      = (_spy_5m_pct * _qqq_5m_pct) > 0   # both moving same direction

    if _spy_extreme:
        _new_event  = "EXTREME"
    elif _spy_triggered and _qqq_confirmed and _same_dir:
        # Layer 3: Both indexes confirmed — classify by news context
        # RVOL gate: if triggering bar is < 0.5x session average volume, it's thin tape
        # (lunch hour, holiday-lite, isolated block trade) — downgrade to SECTOR.
        _RVOL_BROAD_MIN = 0.5
        if _spy_rvol_5m < _RVOL_BROAD_MIN:
            _new_event = "SECTOR"
            logger.info(
                f"📊 BROAD→SECTOR DOWNGRADE: SPY {_spy_5m_pct:+.2f}% + QQQ confirmed "
                f"but RVOL {_spy_rvol_5m:.2f}x < {_RVOL_BROAD_MIN}x — thin tape, "
                f"treating as SECTOR (lunch hour / low-conviction bar)."
            )
        else:
            _ctx       = news.get_active_event_type()    # "WAR_GEO"|"FED_POLICY"|"TECHNICAL"
            _new_event = f"BROAD_{_ctx}"
    elif _spy_triggered:
        _new_event  = "SECTOR"                           # SPY only — sector rotation
    else:
        _new_event  = ""                                 # no trigger this scan

    # Persistence map (scans until auto-clear)
    _PERSIST = {
        "EXTREME":                  3,
        "BROAD_GEO_CONFLICT":       7,   # GEO_CONFLICT: armed conflict / terror — slow to resolve
        "BROAD_GEO_ENERGY":         5,   # GEO_ENERGY: oil shock / supply disruption
        "BROAD_MACRO_MONETARY":     4,   # MACRO_MONETARY: FOMC / central bank — price absorbs fast
        "BROAD_MACRO_CREDIT":       6,   # MACRO_CREDIT: HYG/LQD blowout / bank stress
        "BROAD_MACRO_FX":           5,   # MACRO_FX: USD/JPY dislocation / currency crisis
        "BROAD_MACRO_SYSTEMIC":     8,   # MACRO_SYSTEMIC: pandemic / circuit breaker / cyber — longest
        "BROAD_GLOBAL_ASIA":        3,   # GLOBAL_ASIA: Asia overnight selloff — absorbed quickly
        "BROAD_GLOBAL_EU":          3,   # GLOBAL_EU: EU session selloff absorbed at US open
        "BROAD_TECHNICAL":          3,   # TECHNICAL: price trigger, no cross-asset confirmation
        "SECTOR":                   2,
    }

    _dir_now = "down" if _spy_5m_pct < 0 else "up"

    # MTF FULL BOT AUDIT — JUNE 26 (Rafael decision 2026-06-28): severity rank,
    # mirrors the existing MIN_SCORE bump tiering a few lines below (EXTREME=+3,
    # the 3 hardest macro BROAD types=+2, all other BROAD/SECTOR=+1). Used to
    # prevent a newly-detected MILDER event from prematurely overwriting a
    # currently-active MORE SEVERE event's persistence countdown — e.g. an
    # EXTREME block with 2 scans still owed should not be silently downgraded
    # to a 2-scan SECTOR event just because the very next bar's move was small.
    _EVENT_SEVERITY_RANK = {
        "EXTREME": 3,
        "BROAD_GEO_CONFLICT": 2, "BROAD_MACRO_SYSTEMIC": 2, "BROAD_MACRO_CREDIT": 2,
        "BROAD_GEO_ENERGY": 1, "BROAD_MACRO_MONETARY": 1, "BROAD_MACRO_FX": 1,
        "BROAD_GLOBAL_ASIA": 1, "BROAD_GLOBAL_EU": 1, "BROAD_TECHNICAL": 1,
        "SECTOR": 1,
    }

    _new_rank        = _EVENT_SEVERITY_RANK.get(_new_event, 1) if _new_event else 0
    _active_rank     = _EVENT_SEVERITY_RANK.get(_main._spy_event_type, 0)
    _would_downgrade = bool(
        _new_event
        and _main._spy_risk_active
        and _new_event != _main._spy_event_type
        and _new_rank < _active_rank
    )
    if _would_downgrade:
        logger.info(
            f"📊 Severity-downgrade BLOCKED: new {_new_event} (rank {_new_rank}) "
            f"would have overwritten active {_main._spy_event_type} "
            f"(rank {_active_rank}, {_main._spy_risk_scans_left} scan(s) left) — "
            f"keeping the more severe event's countdown intact and ticking it "
            f"down normally instead."
        )

    if _new_event and not _would_downgrade:
        _persist = _PERSIST.get(_new_event, 3)
        _state_changed = (not _main._spy_risk_active) or (_new_event != _main._spy_event_type)
        if _state_changed:
            if _new_event == "EXTREME":
                logger.critical(
                    f"⛔ EXTREME MOVE: SPY {_spy_5m_pct:+.2f}% | QQQ {_qqq_5m_pct:+.2f}% — "
                    f"ALL new entries blocked this cycle."
                )
            elif _new_event.startswith("BROAD"):
                _block = "longs BLOCKED" if _spy_5m_pct < 0 else "shorts BLOCKED"
                logger.warning(
                    f"⚡ {_new_event} [{_dir_now.upper()}]: "
                    f"SPY {_spy_5m_pct:+.2f}% + QQQ {_qqq_5m_pct:+.2f}% confirmed — "
                    f"{_block}. Persist: {_persist} scans."
                )
            else:
                logger.info(
                    f"📊 SECTOR EVENT [{_dir_now.upper()}]: "
                    f"SPY {_spy_5m_pct:+.2f}% | QQQ flat ({_qqq_5m_pct:+.2f}%) — "
                    f"0.90x size, no direction block, score +1 req. Persist: {_persist} scans."
                )
        # Build #12: warn when direction flips silently on a same-type BROAD event
        # (_state_changed is False when event type is unchanged, so no log fires normally)
        if (_main._spy_risk_active and not _state_changed
                and _dir_now != _main._spy_risk_direction
                and _new_event.startswith("BROAD")):
            _old_block = "longs" if _main._spy_risk_direction == "down" else "shorts"
            _new_block = "longs" if _dir_now == "down" else "shorts"
            logger.warning(
                f"⚡ BROAD DIRECTION FLIP: {_main._spy_event_type} gate reversed "
                f"{_main._spy_risk_direction.upper()} → {_dir_now.upper()} "
                f"(was blocking {_old_block}, NOW blocking {_new_block}) | "
                f"SPY {_spy_5m_pct:+.2f}%"
            )
        _main._spy_risk_active    = True
        _main._spy_risk_direction = _dir_now
        _main._spy_risk_magnitude = _spy_5m_pct
        _main._spy_risk_scans_left = _persist
        _main._spy_event_type     = _new_event
        _main._save_hybrid_state()   # Build #9: persist so restart mid-event restores state
        if _state_changed:
            alert_spy_event(event_type=_new_event, magnitude=_spy_5m_pct,
                            scans_left=_persist)

    elif _main._spy_risk_active:
        # Reached when there's no new trigger this cycle, OR a milder new
        # trigger was just blocked from downgrading the active event above
        # (_would_downgrade) — both cases tick the active event's own
        # countdown down normally rather than resetting it.
        # Build #11: suppress countdown during a data outage — don't let yfinance
        # downtime silently clear an EXTREME or BROAD_GEO_CONFLICT block.
        if _main._spy_fetch_failures >= 3:
            logger.warning(
                f"⚠️  Decay suppressed [{_main._spy_event_type}]: fetch failure streak "
                f"{_main._spy_fetch_failures}x — {_main._spy_risk_scans_left} scan(s) held."
            )
        else:
            _main._spy_risk_scans_left -= 1
            if _main._spy_risk_scans_left <= 0:
                _prev = _main._spy_event_type
                _main._spy_risk_active    = False
                _main._spy_risk_direction = ""
                _main._spy_risk_magnitude = 0.0
                _main._spy_event_type     = ""
                _main._save_hybrid_state()   # Build #9: persist cleared state
                logger.info(f"✅ Market risk cleared [{_prev}] — returning to normal sizing.")
            else:
                _main._save_hybrid_state()   # Build #9: persist countdown
                logger.info(
                    f"Risk [{_main._spy_event_type}] persisting — {_main._spy_risk_scans_left} scan(s) left. "
                    f"Last trigger: SPY {_main._spy_risk_magnitude:+.2f}%"
                )

    # Layer 4: Continuous size multiplier (Proposal C — no cliff edges)
    _spy_abs = abs(_main._spy_risk_magnitude) if _main._spy_risk_active else 0.0
    if _main._spy_event_type == "EXTREME":
        _spy_risk_mult = 0.50
    elif _main._spy_event_type.startswith("BROAD"):
        # Decays 0.20x per additional 1% beyond the 1.0% trigger
        # 1.0% → 0.85x floor | 1.5% → 0.90x | 2.0% → 0.80x (then EXTREME kicks in)
        # Finding 2 fix: minimum floor 0.85x — a 1.0% BROAD trigger must reduce size.
        _spy_risk_mult = round(max(0.85, 1.0 - (_spy_abs - 1.0) * 0.20), 2)
    elif _main._spy_event_type == "SECTOR":
        _spy_risk_mult = 0.90    # fixed light discount — sector-only, no direction block
    else:
        _spy_risk_mult = 1.0

    # MRI size floor — cross-asset background stress score depresses size independent of SPY gate
    # Replaces binary war state clamp. MRI floor is continuous and severity-scaled.
    _mri_floor = mri.size_floor()
    if _mri_floor < _spy_risk_mult:
        _spy_risk_mult = _mri_floor
        logger.info(
            f"MRI {mri.level()} (score={mri.score()}) — size floor {_mri_floor:.2f}x applied."
        )

    # Breadth size floor — T3 market breadth health score (TraderMonty CSV).
    # Acts as an independent floor (takes min vs current mult, never raises it).
    # WEAKENING/CRITICAL breadth = thinning participation = reduced position size.
    _breadth_floor = _main._last_breadth.get("size_floor", 0.95)
    _breadth_zone  = _main._last_breadth.get("zone", "NEUTRAL")
    if _breadth_floor < _spy_risk_mult:
        _spy_risk_mult = _breadth_floor
        logger.info(
            f"Breadth {_breadth_zone} (score={_main._last_breadth.get('score', 50)}) "
            f"— size floor {_breadth_floor:.2f}x applied."
        )

    # Dynamic MIN_SCORE — raises entry bar when market is stressed
    # Applied as post-filter on run_scan() output (signal_generator is read-only)
    _base_min = config.MIN_LONG_SCORE  # Paper=10 (board vote Apr 7 2026); Live=4

    # Layer 1: SPY event-type gate
    if _main._spy_event_type == "EXTREME":
        _spy_min_score = _base_min + 3     # 12/12 only — near impossible in practice
    elif _main._spy_event_type in ("BROAD_GEO_CONFLICT", "BROAD_MACRO_SYSTEMIC", "BROAD_MACRO_CREDIT"):
        _spy_min_score = _base_min + 2     # 11/12 — hardest macro events, high conviction bar
    elif _main._spy_event_type in (
        "BROAD_GEO_ENERGY", "BROAD_MACRO_MONETARY", "BROAD_MACRO_FX",
        "BROAD_GLOBAL_ASIA", "BROAD_GLOBAL_EU", "BROAD_TECHNICAL",
    ):
        _spy_min_score = _base_min + 1     # 10/12 — macro softblock, elevated bar
    elif _main._spy_event_type == "SECTOR":
        _spy_min_score = _base_min + 1     # 10/12 — light discount, higher bar
    else:
        _spy_min_score = _base_min         # 9/12 — normal

    # Layer 2: VIX overlay — independently raises the bar in extreme VIX environments
    # Elevated VIX: panic volatility masks confluence, signal quality degrades
    # Suppressed VIX (<14): low-vol grind produces false breakouts (Stein 2009)
    # Logic: take the max of SPY-driven and VIX-driven scores — both gates compound
    if _main._last_vix > 30.0:
        _vix_min_score = _base_min + 3   # 12/12 — extreme panic (VIX > 30)
    elif _main._last_vix > 25.0:
        _vix_min_score = _base_min + 2   # 11/12 — elevated fear (VIX 25–30)
    elif 0 < _main._last_vix < 14.0:
        _vix_min_score = _base_min + 2   # 11/12 — low-vol false-breakout zone
    else:
        _vix_min_score = _base_min       # normal VIX range

    # Layer 3: MRI min_score_floor — background cross-asset stress raises entry bar
    # Competes with SPY and VIX layers; final floor is max of all three.
    _mri_min_score = mri.min_score_floor(_base_min)

    # Layer 4: Session P&L — raises entry bar as daily losses accumulate (Build #4)
    # Thresholds relative to kill-switch limit (MAX_DAILY_LOSS_PCT = 15%):
    #   ≥ 3.0% loss (20% of limit) → +1 score (10/12) — early warning, tighter filter
    #   ≥ 7.5% loss (50% of limit) → +2 score (11/12) — halfway to kill, high conviction only
    # Win side: no adjustment — lowering the bar on winning days invites overtrading.
    _session_pnl_pct  = risk.daily_pnl / risk.daily_start_value if risk.daily_start_value > 0 else 0.0
    _session_loss_pct = max(0.0, -_session_pnl_pct)
    if _session_loss_pct >= 0.5 * config.MAX_DAILY_LOSS_PCT:      # ≥7.5% loss
        _pnl_min_score = _base_min + 2    # 11/12
    elif _session_loss_pct >= 0.2 * config.MAX_DAILY_LOSS_PCT:    # ≥3.0% loss
        _pnl_min_score = _base_min + 1    # 10/12
    else:
        _pnl_min_score = _base_min        # no adjustment

    # Layer 5: Market extension — SPY proximity to 52w high + market-top-detector score
    # Two signals are combined — price-based (52w high, real-time) and
    # factor-based (distribution days, leadership, rotation, breadth — weekly cache).
    # Either signal alone can raise the bar; both together compound.
    _ath_min_score = _base_min
    _spy_ath_dist_pct = 99.0   # default: no data = treat as not at ATH
    if _main._spy_52w_high > 0 and _main._spy_last_close > 0:
        _spy_ath_dist_pct = (_main._spy_52w_high - _main._spy_last_close) / _main._spy_52w_high * 100
        if _spy_ath_dist_pct < 1.0:                                # < 1% from ATH
            _ath_min_score = _base_min + 2   # 11/12 — near ATH
        elif _spy_ath_dist_pct < config.ATH_MIN_SCORE_RAISE_PCT:   # 1–2% from ATH
            _ath_min_score = _base_min + 1   # 10/12 — approaching ATH
    # market-top-detector supplement: zone_tier 3+ (Red/Critical only) → +1 additional
    # AWP audit fix (2026-06-30): was >= 2 (Orange). Orange (score~47, VIX=16.5 bull
    # market) combined with 1.7%-from-ATH to push the effective floor to 12/12,
    # blocking all entries for an entire session. Orange is "moderate concern" in a
    # normal bull market — it should not compound with ATH proximity to demand a
    # perfect signal. Board 4/4 + Gro + GAI unanimous: only Red (3+) or Critical (4)
    # should trigger the compound. Orange stands alone as its own +1 if it fires the
    # ATH gate (which now requires within 1%, not 2%).
    if _main._market_top_zone_tier >= 3 and _ath_min_score < _base_min + 2:
        _ath_min_score = min(_base_min + 2, _ath_min_score + 1)

    # Layer 6: Macro regime — structural 1-2 year signal (macro-regime-detector skill)
    # Concentration (late-cycle narrow breadth) or Contraction (recession risk) → +1 or +2
    # Refreshed weekly; falls back to 0 if cache absent/stale.
    _regime_min_score = _base_min + _main._macro_regime_tier   # 0=no change, 1=+1, 2=+2

    # Layer 7: FTD (Follow-Through Day) detector — O'Neil market bottom confirmation.
    # MARKET_IN_CORRECTION → +1 (tighter entry during confirmed downtrend).
    # UPTREND_CONFIRMED / RALLY_ATTEMPT / NO_SETUP → no change.
    # Refreshed pre-market by run_ftd.py (cron); stale cache = neutral.
    _ftd_floor = _base_min + _main._ftd_min_score   # 0=no change, 1=+1

    # Layer 8: GEX (Gamma Exposure) — board vote S50b
    # NEGATIVE GEX = dealers short gamma = momentum amplified = require stronger signal.
    # Raises MIN_SCORE +1 in negative regime (stricter confirmation, not a hard block).
    # Shadow mode (GEX_ENABLED=False): logs regime only, does NOT modify _gex_min_score.
    _gex_min_score = _base_min
    _gex_label     = "UNKNOWN"
    try:                                                  # always read for shadow logging
        from data.gex import get_gex_regime as _get_gex
        _gex_data  = _get_gex("SPY")
        _gex_label = _gex_data.get("label", "UNKNOWN")
        _gex_age   = _gex_data.get("age_minutes")
        # INFO (was debug, 2026-07-03): this is the Layer-8 shadow record — the
        # 30-session review requires it visible at production log level.
        logger.info(
            "GEX Layer8: label=%s raw_gex_m=%.1f flip=%s age=%.0fmin",
            _gex_label,
            _gex_data.get("raw_gex_m", 0.0),
            _gex_data.get("flip_strike"),
            _gex_age if _gex_age is not None else -1,
        )
        if getattr(config, "GEX_ENABLED", False):
            if _gex_label == "NEGATIVE":
                _gex_min_score = _base_min + config.GEX_MIN_SCORE_NEG_BUMP  # +1
    except Exception as _gex_err:
        logger.warning("GEX Layer8 failed (non-fatal): %s", _gex_err)

    _dynamic_min_score = max(
        _spy_min_score, _vix_min_score, _mri_min_score,
        _pnl_min_score, _ath_min_score, _regime_min_score,
        _ftd_floor, _gex_min_score,
    )
    _score_reason = ""   # initialize before conditional assignment
    if _dynamic_min_score > _base_min:
        _score_reason = (
            f"SPY={_main._spy_event_type or 'CLEAR'}/{_spy_min_score} "
            f"VIX={_main._last_vix:.1f}/{_vix_min_score} "
            f"MRI={mri.level()}/{_mri_min_score} "
            f"P&L={_session_pnl_pct:+.1%}/{_pnl_min_score} "
            f"ATH={_spy_ath_dist_pct:.2f}%/{_ath_min_score} "
            f"MTD={_main._market_top_score}/{_main._market_top_zone_tier} "
            f"Regime={_main._macro_regime_label or 'none'}/{_regime_min_score} "
            f"FTD={_main._ftd_combined_state or 'none'}/{_ftd_floor} "
            f"GEX={_gex_label}/{_gex_min_score} "
            f"→ effective={_dynamic_min_score}/12"
        )

    # ── Session P&L feedback — Build #4 ──────────────────────────────────────────
    # Loss side: 1.0x at breakeven → 0.0x at kill-switch limit (linear)
    # Win  side: 1.0x at breakeven → 1.25x cap at +5% gain (linear, saturates at cap)
    # _session_pnl_pct already computed above in Layer 4 block — reused here.
    # Applied to new entries only — exit and partial-exit logic is unaffected.
    if _session_pnl_pct <= 0.0:
        _pnl_size_mult = max(0.0, 1.0 + _session_pnl_pct / config.MAX_DAILY_LOSS_PCT)
    else:
        _pnl_size_mult = min(1.25, 1.0 + _session_pnl_pct * 5.0)
    if _pnl_size_mult != 1.0:
        _pnl_dir = "loss" if _session_pnl_pct < 0 else "gain"
        logger.info(
            f"[Build #4] Session P&L mult: {_pnl_size_mult:.2f}x "
            f"(daily {_pnl_dir}: {_session_pnl_pct:+.1%} | "
            f"kill limit {config.MAX_DAILY_LOSS_PCT:.0%} | P&L {risk.daily_pnl:+.2f})"
        )

    # ── Overnight exposure reducer — shrinks intraday sizing by overnight capital commitment ──
    # overnight notional = sum(entry_price × qty_remaining) for all overnight=True open trades
    # mult = max(0.0, 1.0 − (overnight_notional / portfolio_value))
    # $0 overnight → 1.0x | $500/$1K → 0.5x | fully committed → near 0.0x
    _overnight_notional = sum(
        t.get("entry_price", 0.0) * t.get("qty_remaining", t.get("qty", 0))
        for t in tracker.open_trades.values()
        if t.get("overnight") and t.get("status") == "open" and t.get("entry_price")
    )
    _overnight_size_mult = (
        max(0.0, 1.0 - (_overnight_notional / risk.portfolio_value))
        if risk.portfolio_value > 0 and _overnight_notional > 0
        else 1.0
    )
    # BoD-4: VIX > 35 hard zero — Taleb: at VIX 35+ tails are fat enough that
    # overnight positions are lottery tickets; model assumptions break down.
    # Hard zero overrides the formula — no new sizing against open overnight risk.
    _vix_now = regime_data.get("realized_vol", 0) or 0.0
    if _vix_now > 35 and _overnight_notional > 0:
        logger.warning(
            f"BoD-4 VIX GATE: VIX {_vix_now:.1f} > 35 — overnight_size_mult forced to 0.0 "
            f"(was {_overnight_size_mult:.2f}x). No new sizing against open overnight exposure."
        )
        _overnight_size_mult = 0.0
    elif _overnight_notional > 0:
        logger.info(
            f"Overnight exposure: ${_overnight_notional:,.0f} notional → "
            f"intraday size mult {_overnight_size_mult:.2f}x "
            f"({_overnight_notional / risk.portfolio_value:.1%} of ${risk.portfolio_value:,.0f} portfolio)"
        )

    # Combined size multiplier: event risk × regime × TOD × market reaction × session P&L × overnight
    size_multiplier = event_size_mult * regime_size_mult * tod_size_mult * _spy_risk_mult * _pnl_size_mult * _overnight_size_mult

    # ── Partial exits + trailing stops on open positions ─────────────────────
    _main.check_partial_exits(tracker, kelly, risk, mri, last_vix=_main._last_vix)

    # ── QHM: weekly check — earnings gate, stop reconciliation ───────────────
    if _main.qhm is not None:
        try:
            _main.qhm.run_weekly_check()  # type: ignore[attr-defined]
        except Exception as _qhm_wk_e:
            logger.warning("QHM run_weekly_check failed: %s", _qhm_wk_e)

    # ── T3: MRI STRESSED+ breakeven push ─────────────────────────────────────
    if mri and mri.level() in ("STRESSED", "HIGH", "CRITICAL") and tracker.open_trades:
        _apply_mri_breakeven_push(tracker, mri)

    # ── Check full exits on open positions ────────────────────────────────────
    closed = _main.check_exits(tracker, risk, mri_level=mri.level() if mri else "NORMAL",
                         kelly=kelly, last_vix=_main._last_vix, gate_state=gate_state)
    if closed:
        logger.info(f"Exited: {closed}")

    # ── RC-4: Non-blocking fill reconciliation pass ───────────────────────────
    # Patches fill_unverified closed trades with actual Alpaca fill prices.
    # poll_secs=0.1 + no_retry=True — fills are 5–15 min old (settled); no blocking risk.
    _run_fill_recon(tracker, kelly=kelly, risk=risk)

    # ── ANOMALY-LOGGING: 5 proactive checks (Point 9 of 10-point plan) ────────
    # Gene Kim / Charity Majors: surface failure patterns before they compound.
    # Runs every cycle during RTH — all checks are fast (dict reads, no I/O).
    try:
        # Check 1: Stop order failure rate
        # If same symbol failed DAY stop on ≥2 consecutive cycles, alert.
        # _main._rth_day_stop_failure_counts tracks consecutive failures per symbol.
        for _sym in list(tracker.open_trades):
            _fail_n = _main._rth_day_stop_failure_counts.get(_sym, 0)
            if _fail_n >= 2:
                logger.critical(
                    f"[ANOMALY-1] [{_sym}] DAY stop has failed {_fail_n} consecutive "
                    f"cycle(s) — position may be unprotected. Check Alpaca immediately."
                )

        # Check 2: Confirm gate stale entries
        # If symbol has ≥3 confirm scans but still hasn't entered, surface it.
        # Include risk counter vs tracker so the diagnostic is self-contained.
        for _sym, _cnt in list(gate_state.entry_confirm_buffer.items()):
            if _cnt >= 3 and _sym not in tracker.open_trades:
                _risk_ct    = risk.open_positions if risk is not None else "?"
                _tracker_ct = len(tracker.open_trades)
                logger.warning(
                    f"[ANOMALY-2] [{_sym}] confirm_gate={_cnt} scans but no entry — "
                    f"risk.open_positions={_risk_ct} vs tracker={_tracker_ct} | "
                    f"MAX_OPEN_POSITIONS={config.MAX_OPEN_POSITIONS}. "
                    f"If risk>{_tracker_ct}, CYCLE-SYNC should resolve next cycle."
                )

        # Check 3: TQI degradation trend
        # If rolling 5-trade avg drops below 30/100, log WARNING.
        # AB-3 Kelly demotion already fires but has no observability alert.
        if len(kelly._tqi_history) >= 5:
            _tqi_recent_avg = sum(kelly._tqi_history[-5:]) / 5
            if _tqi_recent_avg < 30:
                logger.warning(
                    f"[ANOMALY-3] TQI degradation: last-5-trade avg={_tqi_recent_avg:.1f}/100 "
                    f"< 30 — review exit discipline and stop placement."
                )

        # Check 4: MRI vs news_monitor divergence
        # HALT/STRESSED+ MRI while news_size_mult == 1.0 (CLEAR) suggests the
        # news_monitor and MRI are reading different data sources.
        _mri_lvl = mri.level() if mri else "NORMAL"
        if _mri_lvl in ("HALT", "STRESSED+") and news_size_mult >= 1.0:
            logger.warning(
                f"[ANOMALY-4] MRI={_mri_lvl} but news_size_mult={news_size_mult:.2f} (CLEAR) "
                f"— MRI and news_monitor diverge. Verify cross-asset feed and news feed."
            )

        # Check 5: Fill price polling consistently returning fallback
        # If _fetch_actual_fill_price returned entry_price (fallback) 3+ times today,
        # the fill polling window may be too short for current Alpaca latency.
        if _get_fill_fallback_count() >= 3:
            logger.warning(
                f"[ANOMALY-5] _fetch_actual_fill_price returned fallback price "
                f"{_get_fill_fallback_count()}x today — fill poll window may be too short. "
                f"P&L records may be inaccurate."
            )
    except Exception as _anomaly_err:
        logger.warning("Anomaly checks failed (non-critical): %s", _anomaly_err)

    # ── NEWS HALT (interim, 2026-07-08): block ALL new entries this cycle ─────
    # Exits/partials/breakeven/fill-recon already ran above → existing positions are
    # managed normally. This return blocks QHM + intraday entries. DELIBERATE DIVERGENCE
    # from EXTREME/BV-5 (which sit BELOW the QHM call and let QHM run): a news-keyword HALT
    # is a max-uncertainty, known-false-positive-prone signal, so it over-blocks QHM by one
    # cycle (costs ~nothing — QHM re-checks next cycle). INTERIM-ONLY — revisit in Build F
    # when the keyword classifier is replaced by a real price/exchange signal; do NOT "fix"
    # this back to QHM-orthogonal without that context. No liquidation.
    if _news_halt_block_entries:
        logger.critical(
            "⛔ NEWS HALT — no new entries this cycle (existing positions managed "
            "normally; no liquidation)."
        )
        _touch_cycle_ts()
        return

    # ── QHM: quarterly hold entries — orthogonal to intraday gates ───────────
    # Pre-planned 3-13 week positions; not blocked by EXTREME/BV-5 (intraday only)
    # Harris/Brandt: 10:05 AM ET gate (liquidity settled) — QHM does not enforce internally
    if _main.qhm is not None and (now.hour * 60 + now.minute) >= (10 * 60 + 5):
        try:
            _main.qhm.maybe_enter_positions()  # type: ignore[attr-defined]
        except Exception as _qhm_entry_e:
            logger.warning("QHM maybe_enter_positions failed: %s", _qhm_entry_e)

    # ── EXTREME: block all new entries ────────────────────────────────────────
    if _main._spy_event_type == "EXTREME":
        logger.critical(
            f"⛔ EXTREME BLOCK: SPY {_main._spy_risk_magnitude:+.2f}% — "
            f"no new entries. Exits and partials managed normally."
        )
        _touch_cycle_ts()
        return

    # ── BV-5: Block entries when MRI=HIGH or CRITICAL ───────────────────
    # Intentional: MRI blocks entries (not just adjusts size/score).
    # STRESSED removed from this hard block 2026-06-25 (commit d81e060,
    # restoring the 2026-06-13 board decision) — STRESSED still raises
    # MIN_SCORE/lowers size via the dynamic-floor layers above, just no
    # longer hard-blocks. HIGH/CRITICAL = 0% WR per live trade log
    # (23 exits, 2026-05-05).
    _bv5_mri = mri.level() if mri else "NORMAL"
    if _bv5_mri in ("HIGH", "CRITICAL"):
        logger.critical(
            f"⛔ BV-5: MRI={_bv5_mri} — no new entries until regime improves. "
            f"Exits and partials managed normally."
        )
        _touch_cycle_ts()
        try:
            from scan_to_html import write_scan_html
            write_scan_html(
                portfolio_value=portfolio_value,
                open_trades=dict(tracker.open_trades),
                spy_event_type=_main._spy_event_type,
                daily_pnl=getattr(risk, "equity_pnl", None) or 0.0,
                tqi_history=list(kelly._tqi_history),
                confirm_gate=gate_state.entry_confirm_buffer,
            )
        except (Exception, SystemExit) as _bv5_scan_e:
            logger.warning(f"scan_results.html write failed (BV-5): {_bv5_scan_e}")
        return

    try:
        signals = run_scan(trade_mode=trade_mode, news_size_mult=_spy_risk_mult)
    finally:
        gc.collect()  # free Pandas BlockManager cyclic refs accumulated during scan

    # ── Dynamic MIN_SCORE post-filter ─────────────────────────────────────────
    # Raises the effective entry bar based on current market stress level.
    # Applied here so signal_generator.py (read-only) needs no changes.
    if _dynamic_min_score > _base_min and signals:
        _pre_filter_count = len(signals)
        signals = [s for s in signals if s.get("score", 0) >= _dynamic_min_score]
        if len(signals) < _pre_filter_count:
            logger.info(
                f"Dynamic MIN_SCORE: raised to {_dynamic_min_score}/12 "
                f"[{_score_reason}] — "
                f"{_pre_filter_count - len(signals)} signal(s) filtered out."
            )

    # Layer 9: 16pt confirmation gate — marginal signals (score == _base_min) require 16pt agreement
    # Data (9 days): score-10 WR=0% (0W/4L), 61% rejected by 16pt system. Score-11/12 unaffected.
    # score_16pt tagged on every signal dict at signal_generator.py:L740 — guaranteed present.
    _pre_16pt = len(signals)
    signals = [
        s for s in signals
        if s.get("score", 0) > _base_min          # score-11/12: pass unconditionally
        or s.get("score_16pt", 0) >= _16PT_MIN     # score-10: require 16pt >= 11
    ]
    if len(signals) < _pre_16pt:
        logger.info(
            "16pt gate (Layer 9): %d marginal signal(s) filtered (score=%d, score_16pt<%d).",
            _pre_16pt - len(signals), _base_min, _16PT_MIN,
        )

    if not signals:
        logger.info("No signals this cycle.")
        _touch_cycle_ts()
        return

    # ── Fix A: Per-cycle shorting recheck — updates if equity crossed $2K mid-session ──
    # B1 fix: _SHORTING_ENABLED now in execution/lifecycle.py — use getter/setter
    try:
        _acct_live  = get_account()
        _eq_live    = float(_acct_live.equity)
        _short_live = getattr(_acct_live, "shorting_enabled", False) and _eq_live >= 2000
        if _short_live != _get_shorting_enabled():
            logger.info(
                f"Shorting {'ENABLED' if _short_live else 'DISABLED'} mid-session "
                f"(equity ${_eq_live:,.2f})"
            )
            _set_shorting_enabled(_short_live)    # B1: lifecycle.py module state
    except Exception as _sle:
        logger.warning(
            "Shorting flag live check failed — keeping existing flag=%s: %s",
            _get_shorting_enabled(), _sle,
        )

    # Filter out short signals if account doesn't support shorting
    if not _get_shorting_enabled():
        before = len(signals)
        signals = [s for s in signals if s.get("direction") != "short"]
        skipped = before - len(signals)
        if skipped:
            logger.info(f"Filtered {skipped} short signals — shorting disabled on this account")

    if not signals:
        logger.info("No LONG signals this cycle (all signals were short — shorting disabled).")
        _touch_cycle_ts()
        return

    # ── Session short ban (Option A: any short hard stop → EOD-expiry ban) ─
    if _get_shorts_banned():
        _sb_before = len(signals)
        signals = [s for s in signals if s.get("direction") != "short"]
        if len(signals) < _sb_before:
            logger.info(
                f"Session short ban active: filtered {_sb_before - len(signals)} "
                f"short signal(s) (hard stop triggered earlier this session)"
            )
        if not signals:
            logger.info("No signals after session short ban filter.")
            _touch_cycle_ts()
            return

    logger.info(f"{len(signals)} signals found. Top 3:")
    for s in signals[:3]:
        logger.info(
            f"  {s['symbol']:6s} {s['direction'].upper():5s} "
            f"score={s['score']}/{s['max_score']}  bias={s['bias']}"
        )

    # ── Execute entries ───────────────────────────────────────────────────────────────────────────
    entered = _main.execute_entries(signals, risk, tracker, kelly, mri, size_multiplier, _spy_risk_mult,
                              vix=regime_data.get("realized_vol", 0),
                              spy_risk_direction=_main._spy_risk_direction,
                              gate_state=gate_state)
    if entered:
        logger.info(f"Entered: {entered}")

    kelly.print_summary()
    tracker.print_stats()
    risk.update_daily_pnl_from_alpaca()

    # Write status snapshot for dashboard — read by news terminal + panels
    try:
        Path(_main.LOG_DIR).mkdir(exist_ok=True)
        news_summary = news.get_summary() if news else {}
        _composite = regime.get_composite_regime()
        # Count today's real errors/warnings from the log
        _today_str  = datetime.now(ET).strftime("%Y-%m-%d")
        _err_count  = 0
        _warn_count = 0
        _err_samples = []
        _NOISE = ("Finnhub", "Truth Social", "news_monitor", "Shorting:",  # type: ignore[assignment]
                  "too many requests", "not allowed to short", "position size < 1",
                  "already has Alpaca")
        try:
            _log_path = str(_PROJECT_ROOT / "logs" / "mtf_bot.log")  # RC-2 fix
            with open(_log_path) as _lf:
                for _line in _lf:
                    if _today_str not in _line[:20]:
                        continue
                    if " ERROR " in _line:
                        _err_count += 1
                        if len(_err_samples) < 5:
                            _msg_start = _line.find(" ERROR ")
                            _err_samples.append(_line[_msg_start+7:].strip()[:120])
                    elif " WARNING " in _line and not any(x in _line for x in _NOISE):
                        _warn_count += 1
        except Exception as _lpe:
            logger.warning(f"Log file parse for bot_status failed — error/warning counts may be 0: {_lpe}")
        status = {
            "timestamp":        datetime.now(ET).isoformat(),
            "news_summary":     news_summary,
            "news_items":       news_summary.get("news_items", []),
            "risk_summary":     risk.summary(),
            "trade_stats":      tracker.get_stats(),
            "regime_score":     _composite.get("composite_regime"),
            "vix_term_ratio":   _composite.get("vix_term_ratio"),
            "term_label":       _composite.get("term_label"),
            "spy_vs_50sma_pct": _composite.get("spy_vs_50sma_pct"),
            "spy_rvol_20d":     _composite.get("spy_rvol_20d"),
            "errors_today":     _err_count,
            "warnings_today":   _warn_count,
            "error_samples":    _err_samples,
        }
        from state.persistence import write_bot_status
        write_bot_status(status)
    except Exception as _e:
        logger.warning(f"Status write failed: {_e}")

    # ── Write scan_results.html — auto-refreshes open Chrome tab ─────────────
    # INTERIM THROTTLE: write_scan_html() calls scan_to_html.run_scan() internally,
    # doubling RTH cycle time (441–529s vs. designed 200–250s). Throttle to ≤10 min
    # during quiet cycles. Override fires immediately on entries, exits, MRI shift,
    # or SPY event change. Structural fix required: decouple scan_to_html from cycle.
    _SCAN_HTML_INTERVAL_S = 10 * 60
    _scan_html_elapsed = time.perf_counter() - getattr(run_cycle, "_last_scan_html_ts", -9999.0)
    _mri_now = mri.level() if mri else "NORMAL"
    _spy_now = _main._spy_event_type or ""
    _mri_changed = _mri_now != getattr(run_cycle, "_last_scan_html_mri", None)
    _spy_changed = _spy_now != getattr(run_cycle, "_last_scan_html_spy", None)
    _scan_html_override = bool(entered or closed) or _mri_changed or _spy_changed
    if _scan_html_elapsed >= _SCAN_HTML_INTERVAL_S or _scan_html_override:
        run_cycle._last_scan_html_mri = _mri_now  # type: ignore[attr-defined]
        run_cycle._last_scan_html_spy = _spy_now  # type: ignore[attr-defined]
        logger.debug(
            "scan_html: writing (elapsed=%.0fs override=%s mri=%s spy=%s)",
            _scan_html_elapsed, _scan_html_override, _mri_now, _spy_now,
        )
        try:
            from scan_to_html import write_scan_html
            write_scan_html(
                portfolio_value=portfolio_value,
                open_trades=dict(tracker.open_trades),
                spy_event_type=_main._spy_event_type,
                daily_pnl=getattr(risk, "equity_pnl", None) or 0.0,   # P1-PNL-AUDIT: equity delta matches dashboard formula
                tqi_history=list(kelly._tqi_history),
                confirm_gate=gate_state.entry_confirm_buffer,
            )
        except (Exception, SystemExit) as _e:
            logger.warning(f"scan_results.html write failed: {_e}")
        finally:
            run_cycle._last_scan_html_ts = time.perf_counter()  # type: ignore[attr-defined]
    else:
        logger.debug(
            "scan_html: throttled (elapsed=%.0fs/%ds override=%s)",
            _scan_html_elapsed, _SCAN_HTML_INTERVAL_S, _scan_html_override,
        )

    # ── Refresh dashboard.html — live account snapshot every cycle ───────────
    try:
        from generate_dashboard import generate as _gen_dash
        _gen_dash()
    except (Exception, SystemExit) as _de:
        logger.warning("dashboard refresh failed (non-critical): %s", _de)

    # ── Periodic EOD flush — write snapshot every cycle so data survives crashes ─
    try:
        _rth_eod_date = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        _rth_eod_path = _PROJECT_ROOT / "logs" / f"eod_{_rth_eod_date}.json"
        _rth_reconciled = False
        try:
            if _rth_eod_path.exists():
                _rth_reconciled = bool(json.loads(_rth_eod_path.read_text()).get("_reconcile_ts"))
        except Exception as _rce:
            logger.warning("RTH EOD sentinel check failed — assuming unreconciled: %s", _rce)
        if not _rth_reconciled:
            tracker.write_eod_summary(kelly_sizer=kelly)
    except Exception as _e:
        logger.warning(f"Periodic EOD flush failed — state may not survive crash: {_e}")

    # ── Spawn weekly_review.html — once after 1:15 PM PST per day (H-7) ──────
    try:
        import subprocess as _sp
        from zoneinfo import ZoneInfo as _ZI
        # global already declared in AH spawn block earlier in this function (U-2)
        _dt  = datetime  # module-level import
        _pdt = _ZI("America/Los_Angeles")
        _now_pdt = _dt.now(_pdt)
        _today_pst = _now_pdt.date()
        # Only spawn after 1:15 PM PST and only once per calendar day
        _past_spawn_window = (
            (_now_pdt.hour > 13) or
            (_now_pdt.hour == 13 and _now_pdt.minute >= 15)   # 1:15 PM PST = 4:15 PM ET (after EOD writes)
        )
        # Allow re-spawn if today's EOD file was written AFTER our last spawn
        # (handles the case where EOD writes completed after the first spawn ran)
        _eod_path = str(_PROJECT_ROOT / "logs" / f"eod_{_today_pst}.json")  # RC-2 fix
        _eod_newer = False
        try:
            import time as _wr_t
            if os.path.exists(_eod_path):
                _eod_mtime = os.path.getmtime(_eod_path)
                _spawn_ts  = getattr(_wr_t, "_last_wr_spawn_ts", 0)
                _eod_newer = _eod_mtime > _spawn_ts
        except Exception as _mte2:
            logger.info("RTH spawn EOD mtime check failed: %s", _mte2)
        if _past_spawn_window and (_main._last_weekly_review_spawn_date != _today_pst or _eod_newer):
            _is_friday_post_close = (_now_pdt.weekday() == 4)
            _cmd = [sys.executable,
                    str(_PROJECT_ROOT / "weekly_review.py")]  # RC-2 fix
            if _is_friday_post_close:
                _cmd.append("--analyze")
            _sp.Popen(_cmd, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            _main._last_weekly_review_spawn_date = _today_pst
            import time as _wr_t2; _wr_t2._last_wr_spawn_ts = _wr_t2.time()  # type: ignore[attr-defined]
            logger.info(f"weekly_review.py spawned for {_today_pst} (post 1:15 PM PST)")
    except Exception as _e:
        logger.warning(f"weekly_review.py spawn failed: {_e}")

    # Stamp cycle completion time — watchdog thread reads this to detect hangs
    _touch_cycle_ts()

