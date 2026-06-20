# ruff: noqa: E402, E501  — pre-existing: socket.setdefaulttimeout before stdlib imports; long comment lines
"""
main.py — MTF Confluence Bot runner
Usage:
  python main.py                   → full universe, 15-min intraday loop
  python main.py --mode swing      → swing scan, 60-min loop
  python main.py --once            → single scan and exit
  python main.py --symbols AAPL MSFT NVDA  → override universe
"""

import os
import sys
import gc
import time
import fcntl
import socket
import concurrent.futures
# threading removed — watchdog thread extracted to monitoring/watchdog.py (Phase 2)
import argparse
import json
import logging

# ── Global socket timeout ─────────────────────────────────────────────────────
# Caps ALL socket operations (Alpaca SDK/requests, yfinance) at 15s.
# Alpaca SDK uses requests.Session (not httpx) — this DOES cap those calls.
# 15s is sufficient for any normal Alpaca REST round-trip (<500ms typical).
# Prevents indefinite TCP hangs that silently block the scan cycle and trip
# the watchdog. Individual callers may set shorter timeouts — this is a floor.
socket.setdefaulttimeout(15)
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ── Phase 0.5: Entry-gate state container ────────────────────────────────────
@dataclass
class GateState:
    """Mutable per-session entry-gate state.

    Phase 0.5: replaces _conviction_streak / _entry_confirm_buffer module globals.
    Container-only — no methods. Single instance created in main(), passed through
    run_cycle() -> execute_entries() + check_exits() on every cycle.
    Phase 2: extracts to strategy/scoring.py.
    """
    conviction_streak:    dict = field(default_factory=dict)   # {symbol: int}
    entry_confirm_buffer: dict = field(default_factory=dict)   # {symbol: int}

# _SHORTING_ENABLED moved to execution/lifecycle.py (Phase 2 B1 fix)
_premarket_movers_cache: list = []  # H-2: persists pre-market mover list into RTH
_last_weekly_review_spawn_date: date | None = None  # H-7: spawn weekly_review only once per day
_last_eod_summary_date: date | None = None          # AH dedup: write_eod_summary fires once per calendar day
# _rth_day_stops_submitted_dates moved to execution/gtc_manager.py (Phase 2)
# _halt_entries_for_session moved to events/handlers.py (Phase 2)
_last_daily_reset_date: date | None = None       # C-4: track ET date of last daily kill-switch reset

# ── SPY Market Reaction Gate ──────────────────────────────────────────────────
# Source of truth for intraday risk. Driven by SPY 5-min bar-over-bar movement.
# Updated each scan cycle in run_cycle(). Replaces all keyword-based news sizing.
#
# Trigger: |SPY_close_t - SPY_close_t-1| / SPY_close_t-1 > 1.0%
# RISK_DN  → SPY dropped >1% in last 5-min bar → block new longs
# RISK_UP  → SPY surged >1% in last 5-min bar  → block new shorts
# EXTREME  → |move| > 2.0%                      → block all new entries
# Clears after 3 consecutive clean scans (<1% move) — ~15 minutes
_spy_risk_active:     bool  = False
_spy_risk_direction:  str   = ""     # "down" | "up" | ""
_spy_risk_magnitude:  float = 0.0    # signed % that triggered (e.g. -1.34)
_spy_risk_scans_left: int   = 0      # countdown to auto-clear
_spy_event_type:      str   = ""     # "SECTOR"|"BROAD_GEO_CONFLICT"|"BROAD_GEO_ENERGY"|"BROAD_MACRO_MONETARY"|
                                     # "BROAD_MACRO_CREDIT"|"BROAD_MACRO_FX"|"BROAD_MACRO_SYSTEMIC"|
                                     # "BROAD_GLOBAL_ASIA"|"BROAD_GLOBAL_EU"|"BROAD_TECHNICAL"|"EXTREME"|""
# _conviction_streak / _entry_confirm_buffer removed — now GateState (Phase 0.5)
# _tqi_history removed — now KellySizer._tqi_history (Phase 0.5)
_hybrid_state_loaded: bool = False  # True after first run_cycle() load attempt — prevents repeated disk reads
_spy_fetch_failures:  int  = 0      # consecutive SPY/QQQ yfinance fetch failures — suppresses decay at ≥3
_spy_fetch_alert_sent: bool = False  # T1 fail-streak one-time flag; re-arms on recovery
_last_vix:            float = 0.0  # AB-6R: most recent VIX reading — updated each run_cycle(), read by check_partial_exits()
_kill_switch_alerted: bool  = False  # Alerting: fire kill switch push once per session
# _partial_fail_counts moved to execution/lifecycle.py (Phase 2 B1 fix)
_last_breadth:        dict  = {"score": 50, "zone": "NEUTRAL", "size_floor": 0.95}  # T3 breadth score, refreshed pre-market
_analyst_sentiment:   dict  = {}     # {symbol: {"signal": "BULLISH|NEUTRAL|BEARISH", ...}}, refreshed pre-market
_spy_52w_high:        float = 0.0    # SPY rolling 52-week high close — refreshed pre-market; ATH proximity gate
_spy_last_close:      float = 0.0    # most recent SPY 5m bar close — updated each cycle; ATH Layer 5
_sym_52w_high_cache:  dict  = {}     # {symbol: 52w high close} — per-symbol, refreshed pre-market alongside SPY
_market_top_score:    int   = 0      # market-top-detector composite 0-100; loaded at startup from logs/market_top_latest.json
_market_top_zone_tier: int  = 0      # 0=green…4=critical; Layer 5 supplement
_macro_regime_label:  str   = ""     # macro-regime-detector label; loaded at startup from logs/macro_regime_latest.json
_macro_regime_tier:   int   = 0      # 0=neutral, 1=caution, 2=high-risk; Layer 6
_ftd_combined_state:  str   = ""     # FTD detector combined_state; loaded at startup from data/cache/ftd_state.json
_ftd_min_score:       int   = 0      # Layer 7: +1 when MARKET_IN_CORRECTION, else 0
# ORB gate state — board vote 2026-05-17, 26/26 unanimous
_orb_high:            float = 0.0    # SPY opening-range high (9:30–9:44 ET), set at 9:55 AM
_orb_low:             float = 0.0    # SPY opening-range low (9:30–9:44 ET), set at 9:55 AM
_orb_computed_date:   str   = ""     # "YYYY-MM-DD" on which ORB was computed this session
_orb_feed_failed:     bool  = False  # True if SPY bar fetch failed → BLOCK_ALL entries
# Cycle watchdog — extracted to monitoring/watchdog.py (Phase 2)
from monitoring.watchdog import touch_cycle_ts as _touch_cycle_ts

# _feed_age_history + _systemic_stale_alerted moved to execution/lifecycle.py (Phase 2 B1 fix)
# _rth_day_stop_failure_counts moved to execution/gtc_manager.py (Phase 2)
from execution.gtc_manager import get_failure_counts as _get_gtc_failure_counts
_rth_day_stop_failure_counts = _get_gtc_failure_counts()  # live reference for ANOMALY-1 reporting
# _fill_fallback_count moved to execution/fill_helpers.py (Phase 2)
from execution.fill_helpers import (
    reset_fill_fallback_count as _reset_fill_fallback_count,
)
# _halt_entries_for_session moved to events/handlers.py (Phase 2)
from events.handlers import (
    safe_close_all as _safe_close_all,
    set_halt_entries as _set_halt_entries,
)
# B1: _SHORTING_ENABLED, _feed_age_history, _systemic_stale_alerted, _partial_fail_counts
# moved to execution/lifecycle.py (Phase 2 Extraction 7)
from execution.lifecycle import (
    set_shorting_enabled as _set_shorting_enabled,
    reset_partial_fail_counts as _reset_partial_fail_counts,
)
from execution.orphan_manager import (
    get_tod_phase as _get_tod_phase,
    cancel_and_reconcile_gtc_stops as _cancel_and_reconcile_gtc_stops,
    reconcile_positions as _reconcile_positions,
)

# ── VOTE-4: SPY 200d MA for overnight size reduction (board-approved 2026-04-20) ──
_spy_200d_ma:      float = 0.0   # SPY 200-day MA close; refreshed once per calendar day at cycle start
_spy_200d_ma_date: str   = ""    # ET date of last _spy_200d_ma fetch; prevents redundant daily fetches

# ── Overnight entry feature ───────────────────────────────────────────────────
# Phase 1: DRY-RUN. Set True only after 5-night dry-run validation passes.
# OVERNIGHT_ENTRIES_ENABLED is resolved from config.py — see module-level gate below.
_OVERNIGHT_CANCEL_CUTOFF  = 9 * 60 + 28  # cancel unfilled orders by 9:28 AM ET (2 min before RTH open)
                                           # Ensures stale overnight limits are dead before the first RTH print.
                                           # If the thesis is still valid at open, the scanner regenerates it fresh.
_OVERNIGHT_ENTRY_START    = 20 * 60    # 8 PM ET — earliest overnight entry window
_OVERNIGHT_MAX_AH_DRIFT   = 0.03       # reject if AH price > 3% from close
_OVERNIGHT_MIN_AH_VOLUME  = 50_000     # Marcus Thorne: reject thin AH tape

# ── Rank 1: Bucket A leverage factors + notional cap ────────────────────────
# Prevents notional Nasdaq/sector exposure from growing unbounded as account scales.
# Cap: max 15% notional exposure per Bucket A position.
# Dollar cap = portfolio * 0.15 / leverage_factor (supersedes flat 5% when tighter).
_BUCKET_A_LEVERAGE = {"TSLL": 1.5, "NVDL": 2.0, "TQQQ": 3.0, "SQQQ": 3.0}
_BUCKET_A_MAX_NOTIONAL_PCT = 0.15  # 15% max notional per position

# ── Bug 8 / Bug 3 fix: Sector correlation map ────────────────────────────────
# Maps each tradeable symbol to a sector label. Used to block a second entry
# in the same sector when one position is already open — prevents concentrated
# correlated exposure (e.g. XOM + CVX long simultaneously into energy selloff).
# Bucket A (leveraged ETFs) are exempt: they may intentionally hedge.
# Symbols not in the map produce a DEBUG warning and are treated as uncorrelated.
# Bug 3 fix (Apr 14 2026): expanded with commonly-traded names that were absent
# and added an explicit debug log when a symbol bypasses the gate as "unknown".
# _SECTOR_MAP moved to execution/trade_engine.py (Phase 2 Extraction 10)

# ── Rank 2: Linear position sizing bounds ───────────────────────────────────
# Replaces the binary 47.5%/95% cliff with smooth interpolation across scores 9→11.
# Score 9 → 47.5% | Score 10 → 71.25% | Score 11+ → 95%
_LINEAR_SCORE_MIN = 10  # maps to BUCKET_B_ALLOCATION_PCT * 0.5  (raised from 9 → 10)
_LINEAR_SCORE_MAX = 11  # maps to BUCKET_B_ALLOCATION_PCT * 1.0
from dotenv import load_dotenv

load_dotenv()

# ── Namespace alias: prevent __main__/main shadow copy ───────────────────────
# entry_logic.py does `import main as _main` (lazy, inside function bodies).
# Without this alias, Python loads main.py a second time under sys.modules['main'],
# creating a shadow namespace that never receives profile overrides.
sys.modules.setdefault("main", sys.modules["__main__"])

import data.bar_cache as _bar_cache
from data.fetcher import fetch_bars
from strategy.run_cycle import run_cycle  # Phase 2 Extraction 9
# Phase 2 Extraction 10 — re-exported so run_cycle.py can access via `import main as _main`
from execution.trade_engine import (  # noqa: F401
    _should_flatten_eod, _too_early,
    _save_hybrid_state, _load_hybrid_state,
    _find_recent_fvgs, _score_fvg_for_signal, _compute_fvg_mult,
    _compute_tqi, _record_partial_tqi, _write_confirm_gate_json, _record_tqi,
    execute_entries, check_partial_exits,
    _submit_gtc_limit_partial, _submit_rth_day_stops, _cancel_open_gtc_orders,
    check_exits, _pdt_htf_gate,
    _check_exits_extended_hours, _reconcile_pending_overnight_orders, _overnight_entry_check,
)
from strategy.volatility_regime import RegimeDetector
from execution.kelly import KellySizer
from execution.broker import (
    get_portfolio_value,
    get_account,
)
from execution.risk_manager import RiskManager
from execution.portfolio_tracker import PortfolioTracker
from execution.quarterly_hold_manager import QuarterlyHoldManager, get_quarterly_hold_symbols
from events.calendar import EventCalendar
from events.earnings_fetcher import fetch_upcoming_earnings
from events.news_monitor import NewsMonitor
from events.macro_risk_index import MacroRiskIndex
import config

# ── Overnight entry feature gate ─────────────────────────────────────────────
# Phase 1: DRY-RUN. Set True only after 5-night dry-run validation passes.
# Board vote required before setting OVERNIGHT_ENTRIES_ENABLED = True in config.py.
# Falls back to False if not defined in config — safe default, no overnight entries.
OVERNIGHT_ENTRIES_ENABLED = bool(getattr(config, "OVERNIGHT_ENTRIES_ENABLED", False))

from alerts import (
    alert_crash,
    alert_startup_test,
    send_slack,
)


# ─── LOGGING ─────────────────────────────────────────────────────────────────

LOG_DIR = str(Path(__file__).resolve().parent / "logs")
os.makedirs(LOG_DIR, exist_ok=True)

from logging.handlers import WatchedFileHandler   # H-1: logrotate create-mode compat; inode check on each emit

_LOG_FILE_PATH = os.path.join(LOG_DIR, "mtf_bot.log")
_rfh = WatchedFileHandler(_LOG_FILE_PATH)          # H-1: auto-detects inode change (logrotate create mode)
_handlers: list = [_rfh]
# H-1b: inode guard — suppress StreamHandler when stdout is redirected to the log file
# (e.g. manual start with >> logs/mtf_bot.log). Path-string comparison is insufficient;
# symlinks would bypass it. os.fstat() uses the already-open FD, immune to rename/symlinks.
try:
    _stdout_ino = os.fstat(sys.stdout.fileno()).st_ino
    _log_ino    = os.stat(_LOG_FILE_PATH).st_ino if os.path.exists(_LOG_FILE_PATH) else -1
    if _stdout_ino != _log_ino:
        _handlers.append(logging.StreamHandler(sys.stdout))
except Exception:
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=_handlers,
    force=True,   # H-1c: always replace handlers — required for clean os.execv() restart path
)
del _LOG_FILE_PATH, _rfh, _handlers
logger = logging.getLogger("main")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)   # P3-3: suppress yfinance internal TypeError/error spam


# All trading functions extracted to execution/trade_engine.py (Phase 2 Extraction 10).
# Imported above: from execution.trade_engine import ...

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    global _kill_switch_alerted, _last_daily_reset_date, _last_weekly_review_spawn_date, OVERNIGHT_ENTRIES_ENABLED  # module-level state mutated by main()
    # _fill_fallback_count moved to execution/fill_helpers.py; _rth_day_stop_failure_counts to gtc_manager.py
    parser = argparse.ArgumentParser(description="Alpaca MTF Confluence Bot")
    parser.add_argument("--mode",    default="intraday", choices=["intraday", "swing"])
    parser.add_argument("--once",    action="store_true", help="Single scan then exit")
    parser.add_argument("--symbols", nargs="+",           help="Override universe with specific tickers")
    parser.add_argument("--profile", default="live",     choices=["live", "paper"],
                        help="live=conservative real-money settings | paper=aggressive test settings")
    args = parser.parse_args()

    # ── Process lockfile — prevent two bot instances running simultaneously ──────────────────────
    # If launchd restarts while a prior instance is still alive, the second instance
    # exits immediately rather than placing duplicate orders.
    _lockfile_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "alpaca_mtf_bot.lock")
    _lockfile = open(_lockfile_path, "w")
    try:
        fcntl.flock(_lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("ERROR: Another bot instance is already running. Exiting.")
        sys.exit(1)
    # P5-M1 root-cause fix: set FD_CLOEXEC so the kernel auto-closes this fd
    # (and releases the flock) when os.execv() replaces the process image.
    #
    # Without FD_CLOEXEC the sequence was:
    #   1. Watchdog calls unlock → close → unlink (best-effort, may raise + be swallowed)
    #   2. os.execv() fires — if unlink failed, OLD inode still exists and inherited
    #      fd still holds LOCK_EX on it
    #   3. New process opens same PATH → same inode → tries flock(LOCK_EX | LOCK_NB)
    #   4. Kernel: same inode, different fd, same process → EAGAIN → "Another bot instance"
    #
    # With FD_CLOEXEC:
    #   1. os.execv() fires — kernel closes the fd automatically
    #   2. flock released (all fds to inode are closed)
    #   3. New process opens the path, acquires fresh lock — no race, no loop
    fcntl.fcntl(_lockfile.fileno(), fcntl.F_SETFD, fcntl.FD_CLOEXEC)

    # ── Cycle watchdog thread — extracted to monitoring/watchdog.py (Phase 2) ───
    from monitoring.watchdog import start_watchdog
    start_watchdog(lockfile_path=_lockfile_path)

    # ── Apply profile overrides to config ────────────────────────────────────────────────────────
    profile = config.PROFILES.get(args.profile, config.PROFILES["live"])
    for key, val in profile.items():
        setattr(config, key, val)
    config.ACTIVE_PROFILE = args.profile

    # Re-read after profile overrides apply (profile may set OVERNIGHT_ENTRIES_ENABLED).
    OVERNIGHT_ENTRIES_ENABLED = bool(getattr(config, "OVERNIGHT_ENTRIES_ENABLED", False))
    if OVERNIGHT_ENTRIES_ENABLED:
        logger.warning(
            "OVERNIGHT_ENTRIES_ENABLED=True — confirm Phase 1 dry-run validation "
            "is complete and board vote was recorded before proceeding. "
            "Profile: %s", args.profile
        )
    else:
        logger.info("OVERNIGHT_ENTRIES_ENABLED=False (profile: %s)", args.profile)

    # ── Kill switch: config.py paper profile = 7% (board vote 2026-04-22, 13-0) ──
    # S50: BoD-3 dead-code removed. Override only fired when profile > 0.15;
    # paper profile is 0.07, so condition was always False. Single source of
    # truth is config.py L243. CLAUDE.md Invariant #6 updated to reflect 7%.
    logger.info(
        "Kill switch: %.0f%% (config.py paper profile, board vote 2026-04-22)",
        config.MAX_DAILY_LOSS_PCT * 100,
    )

    # ── AB-4: Opening buffer — config.py now set to 30, this block is redundant ─
    # P5-H4 fix: TOD_MARKET_OPEN_BUFFER_MINS aligned to 30 in config.py directly.
    # Runtime override removed — single source of truth in config.py.

    # ── Config validation — fail fast on bad parameters ──────────────────────
    config.validate_config()

    trade_mode = (
        config.TradeMode.INTRADAY if args.mode == "intraday"
        else config.TradeMode.SWING
    )
    scan_interval = (
        config.SCAN_INTERVAL_INTRADAY * 60
        if trade_mode == config.TradeMode.INTRADAY
        else config.SCAN_INTERVAL_SWING * 60
    )

    logger.info("=" * 60)
    logger.info(f"  MTF CONFLUENCE BOT | Mode: {args.mode.upper()} | Profile: {args.profile.upper()}")
    logger.info(f"  Scan interval: {scan_interval // 60} min | "
                f"Risk: {config.MAX_PORTFOLIO_RISK_PCT:.0%}/trade | "
                f"Kill switch: {config.MAX_DAILY_LOSS_PCT:.0%}/day | "
                f"Max positions: {config.MAX_OPEN_POSITIONS}")
    logger.info("=" * 60)

    # ── Patch 3: Startup dependency validation (with retries) ─────────────────
    for _attempt in range(3):
        try:
            _startup_acct = get_account()
            _startup_status = str(getattr(_startup_acct, "status", "")).upper()
            if "ACTIVE" not in _startup_status:
                logger.critical(f"Startup validation FAILED: Alpaca account status is '{_startup_status}'. Exiting.")
                sys.exit(1)
            logger.info(f"Startup validation: Alpaca account ACTIVE | equity ${float(getattr(_startup_acct, 'equity', 0)):,.2f}")
            break
        except Exception as _sv_err:
            if _attempt < 2:
                logger.warning(f"Startup validation Alpaca API fetch failed ({_sv_err}) — retrying in 5s...")
                time.sleep(5)
            else:
                logger.critical(f"Startup validation FAILED: cannot reach Alpaca API after 3 attempts — {_sv_err}. Exiting.")
                sys.exit(1)

    for _attempt in range(3):
        try:
            # DATA-2g: replaced yfinance SPY download with fetch_bars (T1 Alpaca Data).
            _spy_sv = fetch_bars("SPY", config.TF_DAILY, num_bars=2)
            if _spy_sv is None or _spy_sv.empty:
                logger.critical("Startup validation FAILED: Alpaca Data SPY download returned empty. Exiting.")
                sys.exit(1)
            logger.info(f"Startup validation: SPY data OK via Alpaca Data ({len(_spy_sv)} bar(s))")
            break
        except Exception as _yf_sv_err:
            if _attempt < 2:
                logger.warning(f"Startup validation data fetch failed ({_yf_sv_err}) — retrying in 5s...")
                time.sleep(5)
            else:
                logger.critical(f"Startup validation FAILED: cannot reach Alpaca Data after 3 attempts — {_yf_sv_err}. Exiting.")
                sys.exit(1)
    # ─────────────────────────────────────────────────────────────────────────

    # ── P2-1: Alert system self-test ─────────────────────────────────────────
    # Fire a test ping at startup so operator knows push delivery is working
    # before the session begins. If it fails AND a transport is configured,
    # log a warning and schedule a background retry thread every 5 min.
    _alert_transport_configured = bool(os.getenv("NTFY_TOPIC", "").strip() or
                                       os.getenv("SLACK_WEBHOOK_URL", "").strip())
    if _alert_transport_configured:
        if alert_startup_test():
            logger.info("P2-1: Alert self-test OK — push delivery confirmed.")
        else:
            logger.warning("[ALERT SYSTEM DOWN] Startup ping failed. Retrying every 5 min in background.")
            import threading as _alert_retry_threading
            def _alert_retry_loop():
                import time as _art
                while True:
                    _art.sleep(300)
                    if alert_startup_test():
                        logger.info("[ALERT SYSTEM RECOVERED] Push delivery restored.")
                        break
                    logger.warning("[ALERT SYSTEM DOWN] Retry ping failed — still no push delivery.")
            _alert_retry_t = _alert_retry_threading.Thread(
                target=_alert_retry_loop, name="alert-retry", daemon=True
            )
            _alert_retry_t.start()
    else:
        logger.debug("P2-1: No alert transport configured — self-test skipped.")
    # ─────────────────────────────────────────────────────────────────────────

    # ── Init ────────────────────────────────────────────────────────────────────────────
    # Use Alpaca's last_equity as the SOD baseline so daily_pnl = equity - last_equity
    # matches the dashboard exactly even after intraday restarts.
    try:
        _init_acct      = get_account()
        portfolio_value = float(_init_acct.equity)
        _last_eq        = float(getattr(_init_acct, "last_equity", portfolio_value))
        # P2-SOD-EQUITY: on gap-down days (>5% below last_equity), use current
        # equity as the SOD baseline so the kill switch threshold is reachable.
        # Without this, a 10% gap-down means kill switch fires at last_equity*0.85
        # — a level already below current equity before the first trade.
        _now_et  = datetime.now(ET)
        _mins_et = _now_et.hour * 60 + _now_et.minute
        _in_rth_open_window = (9 * 60 + 30) <= _mins_et <= (10 * 60)  # first 30 min RTH
        _gap_down_pct = (_last_eq - portfolio_value) / _last_eq if _last_eq > 0 else 0
        if _in_rth_open_window and _gap_down_pct > 0.05:
            _sod_equity = portfolio_value
            logger.warning(
                f"P2-SOD-EQUITY: Gap-down detected ({_gap_down_pct:.1%}) in RTH open window. "
                f"SOD baseline set to current equity ${portfolio_value:,.2f} "
                f"(not last_equity ${_last_eq:,.2f}) to preserve kill switch validity."
            )
        else:
            _sod_equity = _last_eq
    except Exception as _init_acct_err:
        logger.warning(f"Startup account fetch failed, using get_portfolio_value fallback: {_init_acct_err}")
        portfolio_value = get_portfolio_value()
        _sod_equity     = portfolio_value
    risk     = RiskManager(portfolio_value, daily_start_value=_sod_equity)
    tracker  = PortfolioTracker()
    calendar = EventCalendar()
    regime   = RegimeDetector()
    kelly    = KellySizer()
    news     = NewsMonitor()
    mri      = MacroRiskIndex()   # T1-B: cross-asset background stress score

    # ── QHM: broker adapter + instantiation ──────────────────────────────────
    from execution.broker import (
        get_open_position as _qhm_get_pos,
        submit_limit_order as _qhm_submit_limit,
        submit_gtc_stop_order as _qhm_submit_gtc_stop,
        close_position as _qhm_close_pos,
    )
    class _QHMBroker:
        """Thin adapter: maps QHM broker protocol to module-level broker functions."""
        def get_position(self, sym): return _qhm_get_pos(sym)
        def submit_limit_order(self, s, q, side, price, ext=False):
            return _qhm_submit_limit(s, q, side, price, ext)
        def submit_gtc_stop_order(self, s, q, side, price):
            return _qhm_submit_gtc_stop(s, q, side, price)
        def close_position(self, sym): return _qhm_close_pos(sym)

    class _QHMSlackAlerter:
        """Thin adapter: routes QHM alerts to send_slack."""
        def send(self, msg): send_slack(msg)

    qhm = QuarterlyHoldManager(
        broker=_QHMBroker(),
        fmp_client=None,
        alerter=_QHMSlackAlerter(),
        config={},
    )

    # D5: blocking startup refresh — ensures first run_cycle() uses live MRI level,
    # not a decayed-from-disk estimate. 60s ceiling covers yfinance edge-case hangs.
    logger.info("MRI startup refresh: blocking up to 60s for initial level verification.")
    _mri_t0 = time.monotonic()
    _mri_startup_err: Exception | None = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _mri_exec:
        _mri_fut = _mri_exec.submit(mri.refresh, True)
        try:
            _mri_fut.result(timeout=60)
        except concurrent.futures.TimeoutError as _te:
            _mri_startup_err = _te
        except Exception as _ex:
            _mri_startup_err = _ex
    _elapsed = time.monotonic() - _mri_t0
    _final_level = mri.level()
    if _mri_startup_err is None:
        logger.info(
            "MRI startup refresh complete: level=%s score=%s elapsed=%.1fs",
            _final_level, mri.score(), _elapsed,
        )
    else:
        _is_timeout = isinstance(_mri_startup_err, concurrent.futures.TimeoutError)
        logger.error(
            "MRI startup refresh %s after %.1fs — level=%s. "
            "Will self-correct on first scan cycle.",
            "timed out" if _is_timeout else f"failed: {_mri_startup_err!r}",
            _elapsed, _final_level,
        )
        send_slack(
            f":warning: MRI STARTUP {'TIMED OUT' if _is_timeout else 'FAILED'}. "
            f"Bot level={_final_level} — will self-correct on first scan cycle."
        )

    # SCHNEIER-FIX (GTC-PENDING-CANCEL 2026-04-30): Reset pending_cancel cycle counters
    # at every session start. The counter survives os.execv() restarts (persisted via
    # _save_log() inside tracker). A crash at cycle 1 (counter=1) followed by restart
    # causes false escalation: next premarket increments to 2 → CRITICAL fired even if
    # Alpaca's pending_cancel resolved naturally during restart. Resetting to 0 here
    # guarantees each session begins with a clean slate. Board vote: 27-0 YES (2026-04-30).
    for _sym_pc in list(tracker.open_trades.keys()):
        if tracker.open_trades[_sym_pc].get("gtc_pending_cancel_cycles", 0) > 0:
            logger.info(
                f"SCHNEIER-FIX: Resetting gtc_pending_cancel_cycles for {_sym_pc} to 0 at session start."
            )
            tracker.open_trades[_sym_pc]["gtc_pending_cancel_cycles"] = 0

    # BUG-ADV-1: restore _halt_entries_for_session from disk if same calendar day.
    # risk.killed is already restored inside RiskManager.__init__() via _load_kill_state().
    # This block covers the module-level halt flag that os.execv() also resets.
    from execution.risk_manager import _load_kill_state as _read_kill_state
    _ks_restore = _read_kill_state()
    _today_restore_et = datetime.now(ET).strftime("%Y-%m-%d")
    if _ks_restore.get("date") == _today_restore_et and _ks_restore.get("halt_entries"):
        _set_halt_entries(True)   # BUG-ADV-1: moved to events/handlers.py (Phase 2)
        logger.warning(
            "BUG-ADV-1: _halt_entries_for_session restored from disk — "
            "news HALT persists from prior watchdog restart. No new entries will be permitted."
        )

    logger.info(f"Connected | Portfolio: ${portfolio_value:,.2f}")
    logger.info(f"Watchlist: {len(config.WATCHLIST)} core tickers + pre-market movers + ATR filter")

    # ── Skill cache loader — market-top-detector + macro-regime-detector ─────
    # Both caches are written by run_market_top.py / run_macro_regime.py (cron).
    # Loaded once at startup; used in Layer 5 and Layer 6 of _dynamic_min_score.
    # Stale cache (> 7 days) is ignored — falls back to 0/neutral.
    global _market_top_score, _market_top_zone_tier, _macro_regime_label, _macro_regime_tier
    _CACHE_MAX_AGE_DAYS = 7
    _cache_cutoff = datetime.now(ET).timestamp() - _CACHE_MAX_AGE_DAYS * 86400

    _mt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "market_top_latest.json")
    try:
        if os.path.exists(_mt_path) and os.path.getmtime(_mt_path) > _cache_cutoff:
            with open(_mt_path) as _f:
                _mt = json.load(_f)
            _market_top_score     = int(_mt.get("composite_score", 0))
            _market_top_zone_tier = int(_mt.get("zone_tier", 0))
            logger.info(
                f"market-top-detector cache loaded: score={_market_top_score}/100 "
                f"zone={_mt.get('zone','?')} tier={_market_top_zone_tier} "
                f"(generated {_mt.get('generated_at','?')[:10]})"
            )
        else:
            logger.info("market_top_latest.json absent or stale — Layer 5 using 52w-high-only mode.")
    except Exception as _mt_err:
        logger.warning(f"market_top cache load failed: {_mt_err}")

    _mr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "macro_regime_latest.json")
    try:
        if os.path.exists(_mr_path) and os.path.getmtime(_mr_path) > _cache_cutoff:
            with open(_mr_path) as _f:
                _mr = json.load(_f)
            _macro_regime_label = str(_mr.get("regime_label", ""))
            _macro_regime_tier  = int(_mr.get("risk_tier", 0))
            logger.info(
                f"macro-regime-detector cache loaded: label={_macro_regime_label} "
                f"confidence={_mr.get('confidence','?')} tier={_macro_regime_tier} "
                f"(generated {_mr.get('generated_at','?')[:10]})"
            )
        else:
            logger.info("macro_regime_latest.json absent or stale — Layer 6 inactive.")
    except Exception as _mr_err:
        logger.warning(f"macro_regime cache load failed: {_mr_err}")

    # ── Layer 7: FTD detector cache loader ───────────────────────────────────
    # run_ftd.py (pre-market cron) calls ftd-detector skill and caches result.
    # MARKET_IN_CORRECTION → +1 min_score (raises entry bar during confirmed downtrend).
    # Cache max age: 3 days (FTD state changes slowly; stale = safe-default neutral).
    global _ftd_combined_state, _ftd_min_score
    _ftd_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "cache", "ftd_state.json")
    _FTD_CACHE_MAX_DAYS = 3
    _ftd_cache_cutoff   = datetime.now(ET).timestamp() - _FTD_CACHE_MAX_DAYS * 86400
    try:
        if os.path.exists(_ftd_path) and os.path.getmtime(_ftd_path) > _ftd_cache_cutoff:
            with open(_ftd_path) as _f:
                _ftd = json.load(_f)
            _ftd_combined_state = str(_ftd.get("combined_state", ""))
            _ftd_min_score = 1 if _ftd_combined_state == "MARKET_IN_CORRECTION" else 0
            logger.info(
                f"ftd-detector cache loaded: state={_ftd_combined_state} "
                f"quality={_ftd.get('quality_signal','?')} "
                f"min_score_delta=+{_ftd_min_score} "
                f"(generated {_ftd.get('generated_at','?')[:10]})"
            )
        else:
            logger.info("ftd_state.json absent or stale — Layer 7 inactive (neutral).")
    except Exception as _ftd_err:
        logger.warning(f"ftd_state cache load failed: {_ftd_err}")

    # H-1: SIGTERM handler — graceful shutdown on system kill or launchd restart.
    # Positions are NOT auto-closed on SIGTERM (bot may be restarting, not stopping).
    # State is saved so the next instance resumes cleanly from the last known position.
    import signal as _sig_h1
    def _handle_sigterm(signum, frame):
        try:
            _open_syms = list(tracker.open_trades.keys()) if hasattr(tracker, "open_trades") else []
        except Exception as _syms_e:
            logger.debug("SIGTERM: open_syms fallback failed — %s", _syms_e)
            _open_syms = []

        if _open_syms:
            logger.warning(
                f"SIGTERM received (signal {signum}) — saving state and exiting. "
                f"Open positions: {', '.join(_open_syms)}."
            )
        else:
            logger.info(
                f"SIGTERM received (signal {signum}) — no open positions, saving state and exiting."
            )

        _state_save_failed = False
        try:
            tracker._save_log()           # preserve in-memory state (counts, flags, timestamps)
            tracker.write_eod_summary(kelly_sizer=kelly)   # write EOD JSON snapshot
            logger.info("State and EOD snapshot written on SIGTERM.")
        except Exception as _ste:
            logger.warning(f"State save on SIGTERM failed: {_ste}")
            _state_save_failed = True
        try:
            qhm.safe_stop(circuit_breaker=False)  # persist QHM state; SIGTERM may be restart
        except Exception as _qhm_sigterm_e:
            logger.warning("QHM safe_stop failed on SIGTERM: %s", _qhm_sigterm_e)

        if _open_syms or _state_save_failed:
            _reason = f"SIGTERM (signal {signum})"
            if _state_save_failed:
                _reason += " + STATE SAVE FAILED"
            try:
                alert_crash(reason=_reason, open_positions=_open_syms)
            except Exception as _se_alert:
                logger.warning(f"SIGTERM crash alert failed: {_se_alert}")

        try:
            _lockfile.close()
        except Exception as _lk_e:
            logger.debug("SIGTERM: lockfile close failed — %s", _lk_e)
        sys.exit(0)
    _sig_h1.signal(_sig_h1.SIGTERM, _handle_sigterm)
    logger.info("H-1: SIGTERM handler registered.")

    # ── Shorting check ────────────────────────────────────────────────────────────────────────────
    try:
        account = get_account()
        account_equity = float(account.equity)
        # Alpaca requires margin account + min $2,000 equity for short selling.
        # Read the actual account attribute — do not hardcode.
        shorting_enabled_flag = getattr(account, "shorting_enabled", False)
        if shorting_enabled_flag and account_equity >= 2000:
            shorting_on = True
            logger.info(
                f"Shorting: ENABLED (equity ${account_equity:,.2f} >= $2,000) "
                f"— short signals will be traded"
            )
        elif shorting_enabled_flag and account_equity < 2000:
            shorting_on = False
            logger.warning(
                f"Shorting: DISABLED — account equity ${account_equity:,.2f} < $2,000 "
                f"minimum for margin short selling. Short signals will be SKIPPED."
            )
        else:
            shorting_on = False
            logger.warning(
                "Shorting: DISABLED on this Alpaca account — short signals will be "
                "SKIPPED. To enable: Alpaca → Account → Settings → Allow Short Selling"
            )
    except Exception as e:
        shorting_on = False
        logger.warning(f"Could not check shorting status: {e}")

    # B1 fix: _SHORTING_ENABLED now in execution/lifecycle.py — use setter
    _set_shorting_enabled(shorting_on)

    # ── Volatility regime ───────────────────────────────────────────────────────────────────────────
    regime_data = regime.get_regime(force_refresh=True)
    logger.info(f"Vol regime: {regime_data['note']}")

    # ── Event calendar ───────────────────────────────────────────────────────────────────────────
    day_risk = calendar.get_day_risk()
    logger.info(f"Today's risk tier: {day_risk.get('risk', 'CLEAR')} — {day_risk.get('note', '')}")

    week = calendar.get_week_ahead()
    if week:
        logger.info("EVENTS THIS WEEK:")
        for ev in week:
            logger.info(f"  {ev['date']} | {ev['risk'].value.upper()} | {ev['note']}")

    # ── Finnhub economic calendar (live macro events) ─────────────────────────────────
    try:
        finnhub_added = news.refresh_calendar(calendar, days_ahead=30)
        if finnhub_added == -1:
            logger.info("Finnhub calendar: paid-tier endpoint (403) — static calendar active (FOMC Apr 8, CPI Apr 10).")
        elif finnhub_added == 0:
            logger.info("Finnhub calendar: 0 new events returned.")
        else:
            logger.info(f"Finnhub calendar: {finnhub_added} event(s) added from live feed.")
    except Exception as e:
        logger.warning(f"Finnhub calendar refresh failed: {e}")

    # ── FMP economic calendar (DATA-4 ph2) — T2 supplement to static calendar ──
    try:
        from data.fmp_client import get_economic_calendar as _get_econ_cal
        from events.calendar import EventType as _EvType, EventRisk as _EvRisk
        _fmp_events = _get_econ_cal(lookahead_days=45)
        _fmp_added  = 0
        for _ev in _fmp_events:
            _type = getattr(_EvType, _ev["type_str"], _EvType.OTHER)
            _risk = getattr(_EvRisk, _ev["risk_str"], _EvRisk.HIGH_RISK)
            calendar.add_event_dynamic(_ev["date"], _type, _risk, _ev["note"])
            _fmp_added += 1
        logger.info(f"FMP economic calendar: {_fmp_added} event(s) injected (T2)")
    except Exception as e:
        logger.warning(f"FMP economic calendar refresh failed: {e}")

    # ── Earnings calendar ───────────────────────────────────────────────────────────────────────────
    try:
        added = fetch_upcoming_earnings(calendar)
        logger.info(f"Loaded {added} upcoming earnings dates")
    except Exception as e:
        logger.warning(f"Could not load earnings calendar: {e}")

    # ── Kelly stats from prior sessions ────────────────────────────────────────────────────────────
    kelly.print_summary()

    # ── Startup reconciliation ────────────────────────────────────────────────
    # Each step wrapped independently — a slow/failed Alpaca call must not
    # prevent the bot from starting and managing existing positions.
    # 1. Reconcile overnight GTC stops (check if any triggered while bot was offline)
    try:
        _cancel_and_reconcile_gtc_stops(tracker, risk)
        logger.info("GTC reconciliation complete.")
    except Exception as _rec_e:
        logger.error(f"GTC reconciliation failed — proceeding without it: {_rec_e}")
    # 2. Reconcile Alpaca positions vs tracker (detect orphans / external closes)
    try:
        _reconcile_positions(tracker, risk, trade_mode=trade_mode)
        logger.info("Position reconciliation complete.")
    except Exception as _rec_e:
        logger.error(f"Position reconciliation failed — proceeding without it: {_rec_e}")
    # 3. Reconcile any pending overnight DAY limit orders (check fills / cancellations)
    try:
        _reconcile_pending_overnight_orders(tracker, risk)
        logger.info("Pending order reconciliation complete.")
    except Exception as _rec_e:
        logger.error(f"Pending order reconciliation failed — proceeding without it: {_rec_e}")
    # 4. Sync risk.open_positions to actual tracker state after reconciliation
    risk.sync_from_tracker(tracker)
    # 5. QHM: reconcile quarterly hold positions vs Alpaca after risk sync
    try:
        _qhm_result = qhm.reconcile_on_startup()
        logger.info(
            "QHM reconcile: %d symbol(s), %d order(s) verified, "
            "%d stop(s) resubmitted, %d closed externally, %d day orders expired",
            len(_qhm_result.symbols_reconciled), _qhm_result.orders_verified,
            _qhm_result.stops_resubmitted, _qhm_result.positions_closed_externally,
            _qhm_result.day_orders_expired,
        )
        for _rec_err in _qhm_result.errors:
            logger.warning("QHM reconcile error: %s", _rec_err)
    except Exception as _qhm_rec_e:
        logger.error("QHM reconcile_on_startup failed: %s", _qhm_rec_e)
    # 6. QHM: add candidates with not_before_date gate (idempotent; skips already-tracked)
    _today_qhm = datetime.now(ET).date()
    for _sym, _pick_cfg in qhm._thesis_config.items():
        _nbf = _pick_cfg.get("not_before_date", "")
        try:
            _tgt = float(_pick_cfg.get("target_equity_pct", 0.10))
            if _nbf and _today_qhm >= date.fromisoformat(_nbf):
                qhm.add_candidate(_sym, target_equity_pct=_tgt)
        except (ValueError, TypeError) as _nbf_e:
            logger.warning("QHM: not_before_date parse for %s (value=%r) failed: %s",
                           _sym, _nbf, _nbf_e)
    # Log registry after candidates added — shows accurate post-add state
    _qhm_registered = sorted(get_quarterly_hold_symbols())
    if _qhm_registered:
        logger.info("QHM registry: %s blocked for intraday entries", _qhm_registered)
    # 7. P0-STARTUP: Validate risk count against Alpaca live ledger.
    #    sync_from_tracker() reads local JSON — may be stale after crash/external close.
    #    This block is the authoritative override: Alpaca position count wins.
    #    Amendment A-1 (marking individual stale entries) was rejected by board (S42):
    #    record_exit() is the only safe close path — direct status mutation bypasses P&L.
    #    reconcile_positions() above already handles entry-level cleanup.
    try:
        from execution.broker import get_open_positions as _broker_positions
        _live_pos    = _broker_positions()
        _live_count  = len(_live_pos)
        _tracker_count = risk.open_positions
        if _live_count != _tracker_count:
            logger.critical(
                f"P0-STARTUP: Alpaca live positions ({_live_count}) != "
                f"tracker count ({_tracker_count}). "
                f"Overriding to Alpaca-authoritative count."
            )
            if _live_count > _tracker_count:
                for _ in range(_live_count - _tracker_count):
                    risk.register_open()
            else:
                logger.warning(
                    "P0-STARTUP: Position count decreased Alpaca=%d tracker=%d — forcing direct sync.",
                    _live_count, _tracker_count,
                )
                risk.open_positions = _live_count
            # Observability: surface which symbols are causing the discrepancy
            _alpaca_syms  = {pos.symbol for pos in _live_pos}
            _tracker_syms = {s for s, t in tracker.open_trades.items()
                             if t.get("status") != "closed"}
            _untracked = _alpaca_syms - _tracker_syms
            _stale     = _tracker_syms - _alpaca_syms
            if _untracked:
                logger.critical(
                    f"P0-STARTUP: In Alpaca but NOT in tracker: {sorted(_untracked)}. "
                    f"Stops/targets will NOT fire for these — manual intervention required."
                )
            if _stale:
                logger.critical(
                    f"P0-STARTUP: In tracker but NOT in Alpaca: {sorted(_stale)}. "
                    f"Likely externally closed — reconcile_positions() should have handled this."
                )
        else:
            logger.info(
                f"P0-STARTUP: Positions verified — Alpaca={_live_count} == "
                f"tracker={_tracker_count}. OK."
            )
        if risk.open_positions >= config.MAX_OPEN_POSITIONS:
            logger.critical(
                f"P0-STARTUP: Already at MAX positions "
                f"({risk.open_positions}/{config.MAX_OPEN_POSITIONS}). "
                f"Blocking new entries."
            )
            _set_halt_entries(True)
    except ImportError as _p0_import_err:
        logger.critical(
            f"P0-STARTUP: Cannot import get_open_positions ({_p0_import_err}). "
            f"Entries HALTED — manual reconciliation required."
        )
        _set_halt_entries(True)
    except Exception as _p0_err:
        logger.critical(
            f"P0-STARTUP: Alpaca position count validation FAILED ({_p0_err}). "
            f"Entries HALTED — manual reconciliation required."
        )
        _set_halt_entries(True)

    # ── Phase 0.5: GateState — holds conviction_streak + entry_confirm_buffer ──────────────────
    gate_state = GateState()

    # ── Bug 5 fix: restore confirm gate state if bot restarted mid-session ────────────────────
    from state.persistence import read_confirm_gate
    _cg_data = read_confirm_gate()
    if _cg_data:
        gate_state.entry_confirm_buffer.update(_cg_data.get("entry_confirm_buffer", {}))
        gate_state.conviction_streak.update(_cg_data.get("conviction_streak", {}))
        if gate_state.entry_confirm_buffer or gate_state.conviction_streak:
            logger.info(
                f"Confirm gate restored: {len(gate_state.entry_confirm_buffer)} symbol(s) in confirm buffer, "
                f"{len(gate_state.conviction_streak)} in conviction streak."
            )

    # Phase 0.5: TQI history restore moved to KellySizer.__init__._restore_tqi_from_disk()
    # (called unconditionally — DS Condition 4). No main() startup block needed.

    # ── P3: Code version verification — confirms running process loaded latest code ──────────
    try:
        _main_mtime = datetime.fromtimestamp(os.path.getmtime(__file__)).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"Code version: main.py last modified {_main_mtime} | PID {os.getpid()}")
    except Exception as _ver_e:
        logger.debug("code version check failed — %s", _ver_e)

    # ── Startup: load breadth + analyst caches so floors apply immediately ──────────────────
    # Pre-market block refreshes these live; this ensures a cached value is available
    # if the bot restarts during RTH (avoids neutral default for the rest of the session).
    try:
        from data.market_breadth import get_breadth_score as _get_breadth
        _last_breadth.clear()
        _last_breadth.update(_get_breadth())   # uses cache if fresh, else fetches
        logger.info(
            f"Startup breadth: score={_last_breadth['score']}/100 "
            f"zone={_last_breadth['zone']} floor={_last_breadth['size_floor']:.2f}x"
        )
    except Exception as _sb_err:
        logger.warning(f"Startup breadth load failed: {_sb_err}")

    try:
        from data.fmp_client import get_analyst_sentiment as _get_asent
        _wl_syms = list(config.WATCHLIST)
        for _asym in _wl_syms:
            _analyst_sentiment[_asym] = _get_asent(_asym)   # cache hit on most; 1 live call on miss
        _bs = [s for s, v in _analyst_sentiment.items() if v.get("signal") == "BEARISH"]
        if _bs:
            logger.info(f"Startup analyst sentiment: BEARISH={_bs} — these symbols will be skipped at entry")
    except Exception as _sa_err:
        logger.warning(f"Startup analyst sentiment load failed: {_sa_err}")

    # ── Universe ────────────────────────────────────────────────────────────────────────────
    universe_override = [s.upper() for s in args.symbols] if args.symbols else None
    if universe_override:
        logger.info(f"Universe override: {universe_override}")

    # ── Daily reset ───────────────────────────────────────────────────────────────────────────
    # H-1: use _sod_equity (Alpaca last_equity = SOD baseline) not current portfolio_value.
    # On mid-session restart, portfolio_value may already reflect intraday gains/losses —
    # passing it here overwrites daily_start_value with the wrong floor, corrupting the
    # kill switch. daily_pnl zeroes here and re-accumulates via register_close() going
    # forward; equity_pnl property covers the gap for the kill switch worst-case check.
    risk.reset_daily(_sod_equity)
    _last_daily_reset_date = datetime.now(ET).date()   # prevent double-reset on first loop iteration

    # ── Run ────────────────────────────────────────────────────────────────────────────
    if args.once:
        run_cycle(risk, tracker, calendar, regime, kelly, news, mri, trade_mode, universe_override,
                  gate_state=gate_state)
        tracker.print_stats()
        return

    logger.info(f"Bot running — scanning every {scan_interval // 60} min RTH | 10 min pre-market. Ctrl+C to stop.")

    # ── Connection heartbeat state ────────────────────────────────────────────
    _health_fail_count = 0

    while True:
        try:
            # ── C-4: Daily kill-switch reset — once per ET calendar day ────────
            _today_et = datetime.now(ET).date()
            if _last_daily_reset_date != _today_et:
                # P5-H1 fix: decouple buffer/state clear from the network call.
                # Step 1 — clear session state immediately. No Alpaca dependency.
                # These MUST clear at day boundary regardless of API availability.
                # Mark date first so the retry loop cannot fire a second clear.
                _last_daily_reset_date = _today_et
                _set_halt_entries(False)         # H-6: Phase 2 — events/handlers.py
                _kill_switch_alerted  = False
                gate_state.conviction_streak.clear()     # 12/12 conviction streak — never carry across days
                gate_state.entry_confirm_buffer.clear()  # BoD-1 2-scan gate — stale streak = false entry
                _last_weekly_review_spawn_date = None    # RC9: dedup guard must reset at day boundary
                _reset_partial_fail_counts()     # RC11: Phase 2 — execution/lifecycle.py
                _reset_fill_fallback_count()     # ANOMALY-5: Phase 2 — execution/fill_helpers.py
                from execution.gtc_manager import reset_daily as _gtc_reset_daily
                _gtc_reset_daily()  # ANOMALY-1: clears per-symbol stop failure counts
                # RAM-DRAIN-1 (2026-04-30): Prune expired ATR cache entries — delegated to data.bar_cache.
                # Each entry holds a pandas DataFrame (~2-5MB). Without pruning,
                # RSS grows ~1.2MB/min during RTH. ts=time.time() float; TTL=4h.
                # Board vote: 27-0 YES (2026-04-30).
                # ATR cache prune — delegated to data.bar_cache (Phase 2)
                _bar_cache.prune_atr()
                from state.persistence import clear_confirm_gate
                clear_confirm_gate()
                # ORB gate state reset — clears prior day's range so fresh compute fires at 9:55 AM
                _orb_high          = 0.0
                _orb_low           = 0.0
                _orb_computed_date = ""
                _orb_feed_failed   = False
                # Step 2 — portfolio baseline reset. Network call; allowed to fail softly.
                # If it fails, risk manager uses yesterday's baseline (off by 1 day — acceptable).
                try:
                    _pv_reset = get_portfolio_value()
                    risk.reset_daily(_pv_reset)
                    logger.info(
                        f"Daily reset fired for {_today_et} — "
                        f"kill switch cleared, P/L baseline reset to ${_pv_reset:,.2f}, "
                        f"conviction/confirm buffers cleared"
                    )
                except Exception as _reset_err:
                    logger.warning(
                        f"Daily reset: buffers/state cleared for {_today_et}, "
                        f"portfolio baseline reset failed (using yesterday's) — {_reset_err}"
                    )
            # ── Connection heartbeat (RTH only: 9:30 AM–4:00 PM ET = 6:30 AM–1:00 PM PST) ──
            now_et  = datetime.now(ET)
            mins_et = now_et.hour * 60 + now_et.minute
            is_rth  = (9 * 60 + 30) <= mins_et < (16 * 60)

            # P5-N1 FIX: Stamp watchdog baseline at the START of every loop
            # iteration so the watchdog measures only execution time, not sleep
            # time. Without this, a 30-min overnight sleep causes the watchdog
            # to fire at 9:30 AM before the first RTH cycle completes.
            _touch_cycle_ts()

            if is_rth:
                try:
                    get_account()
                    _health_fail_count = 0
                except Exception as _hb_err:
                    _health_fail_count += 1
                    logger.warning(
                        f"Heartbeat fail {_health_fail_count}/3: {_hb_err}"
                    )
                    if _health_fail_count >= 3:
                        logger.critical(
                            "3 consecutive heartbeat failures — restarting bot."
                        )
                        try:
                            tracker.write_eod_summary(kelly_sizer=kelly)
                        except Exception as _hre:
                            logger.warning(f"EOD write failed before heartbeat restart: {_hre}")
                        try:                          # H-4: unlock + close + delete before execv
                            fcntl.flock(_lockfile, fcntl.LOCK_UN)
                            _lockfile.close()
                            os.unlink(_lockfile_path)
                        except Exception as _hb_e:
                            logger.debug("heartbeat: lockfile close failed — %s", _hb_e)
                        os.chdir(os.path.dirname(os.path.abspath(__file__)))
                        os.execv(sys.executable, [sys.executable] + sys.argv)

            _cycle_t0 = time.perf_counter()
            run_cycle(risk, tracker, calendar, regime, kelly, news, mri, trade_mode, universe_override,
                      gate_state=gate_state)
            _cycle_dur = time.perf_counter() - _cycle_t0
            # Release Python heap after each cycle — prevents heap fragmentation OOM (DS audit 2026-04-29)
            _gc_t0 = time.perf_counter()
            collected = gc.collect()
            _gc_dur = time.perf_counter() - _gc_t0
            if _gc_dur > 0.2:
                logger.warning(f"gc.collect() took {_gc_dur*1000:.1f}ms, collected {collected} objects — heap may be growing")
            _watchdog_headroom = (12 * 60) - _cycle_dur   # 12-min RTH watchdog threshold
            _slow_flag = " ⚠ SLOW CYCLE" if _cycle_dur > 7 * 60 else ""
            logger.info(
                f"[CYCLE] duration={_cycle_dur:.0f}s | phase={_get_tod_phase()} | "
                f"watchdog_headroom={max(0, _watchdog_headroom):.0f}s{_slow_flag}"
            )

            # ── Sleep interval by session phase ─────────────────────────────
            now_et  = datetime.now(ET)
            mins_et = now_et.hour * 60 + now_et.minute
            is_premarket = (8 * 60 + 45) <= mins_et < (9 * 60 + 30)
            is_rth       = (9 * 60 + 30) <= mins_et < (16 * 60)
            if is_rth:
                sleep_secs = scan_interval        # 5 min during RTH
            elif is_premarket:
                sleep_secs = 10 * 60              # 10 min pre-market
            else:
                sleep_secs = 30 * 60              # 30 min overnight (low activity)
            time.sleep(sleep_secs)

        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            _safe_close_all(tracker, risk, circuit_breaker=True,
                            mri_level=mri.level() if mri else "NORMAL")  # H-6 / C-2: user shutdown = close all
            try:
                qhm.safe_stop(circuit_breaker=False)  # persist QHM state on user shutdown
            except Exception as _qhm_kb_e:
                logger.warning("QHM safe_stop failed on shutdown: %s", _qhm_kb_e)
            try:
                tracker.write_eod_summary(kelly_sizer=kelly)
                logger.info("EOD snapshot written on shutdown.")
            except Exception as _e:
                logger.warning(f"EOD snapshot on shutdown failed: {_e}")
            tracker.print_stats()
            kelly.print_summary()
            break

        except Exception as e:
            logger.error(f"Loop error: {e}", exc_info=True)
            time.sleep(60)


if __name__ == "__main__":
    main()
