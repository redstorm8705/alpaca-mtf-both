"""
execution/portfolio_tracker.py
Tracks open trades, P&L, day trades, and trade history.

Persistence:
  trade_log.json        — open + closed trades (atomic write with .bak)
  logs/day_trades.json  — rolling PDT counter (atomic write, survives restarts)
  logs/kelly_stats.json — Kelly win/loss data (written by kelly.py)
"""

import json
import os
import sys
import logging
import shutil
import ssl
import tempfile
import urllib.request
import urllib.parse
import time as _time_mod
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# macOS Python ships without system CA certs —
# use certifi bundle (same pattern as alerts.py)
try:
    import certifi as _certifi
    _SSL_CTX = ssl.create_default_context(cafile=_certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

_ET = ZoneInfo("America/New_York")
_PT = ZoneInfo("America/Los_Angeles")

# ── numpy-safe JSON encoder ───────────────────────────────────────────────────
try:
    import numpy as _np
    _NP_INTEGER  = _np.integer
    _NP_FLOATING = _np.floating
except ImportError:  # numpy not installed (shouldn't happen)
    _NP_INTEGER  = ()  # type: ignore[misc,assignment]
    _NP_FLOATING = ()  # type: ignore[misc,assignment]

class _BotEncoder(json.JSONEncoder):
    """Converts numpy scalar types to Python native. Raises TypeError for unknowns —
    no silent corruption via default=str."""
    def default(self, obj):
        if isinstance(obj, _NP_INTEGER):
            return int(obj)
        if isinstance(obj, _NP_FLOATING):
            return float(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        try:
            import uuid as _uuid
            if isinstance(obj, _uuid.UUID):
                return str(obj)
        except ImportError:
            pass
        return super().default(obj)  # raises — surfaces unknown types immediately

logger = logging.getLogger(__name__)

_ROOT           = Path(__file__).parent.parent.resolve()   # alpaca-mtf-bot_FINAL/
sys.path.insert(0, str(_ROOT))
try:
    from trade_logger import log_event as _log_event, _STOP_REASONS as _LOG_STOP_REASONS
except ImportError:
    def _log_event(*a, **kw): pass   # type: ignore[misc]  # fail-safe: logging never breaks the bot
    _LOG_STOP_REASONS = frozenset()
TRADE_LOG_FILE     = _ROOT / "trade_log.json"
DAY_TRADES_FILE    = _ROOT / "logs" / "day_trades.json"

# Phase 2: Alpaca fills as EOD P&L authority (board vote 2026-04-19, 26-0)
_ALPACA_PAPER_BASE = "https://paper-api.alpaca.markets"
_LOTS_STATE_FILE        = _ROOT / "data" / "state" / "open_lots_prior_day.json"
_DRIFT_ALERT_FILE       = _ROOT / "logs" / "last_drift_alert.json"


def _load_drift_alert_date() -> str:
    try:
        if _DRIFT_ALERT_FILE.exists():
            return json.loads(_DRIFT_ALERT_FILE.read_text()).get("date", "")
    except Exception as _e:
        logger.warning(
            "_load_drift_alert_date: load failed (%s) — drift dedup reset", _e
        )
    return ""


_last_eod_drift_alert_date: str = _load_drift_alert_date()  # persisted across restarts
_market_holidays_fallback_logged: bool = False  # one-time CRITICAL on import failure


# ── Atomic JSON write helper ──────────────────────────────────────────────────

def _atomic_write(filepath: Path, data: dict):
    """
    Write JSON atomically:
      1. Write to <filepath>.tmp
      2. fsync to ensure disk flush
      3. os.replace() — atomic on POSIX/macOS (file is never partially written)
      4. Keep previous version as .bak

    Prevents trade_log.json corruption on crash/power loss.
    """
    bak_path = Path(str(filepath) + ".bak")
    tmp_path = None
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=filepath.parent, suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, cls=_BotEncoder)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError as _e:
                logger.warning("_atomic_write fd.close failed (fd leak risk): %s", _e)
            raise
        if filepath.exists():
            shutil.copy2(str(filepath), str(bak_path))
        os.replace(str(tmp_path), str(filepath))
    except Exception as e:
        logger.error(f"Atomic write failed for {filepath}: {e}")
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception as _e:
                logger.warning(
                    "_atomic_write tmp cleanup failed for %s: %s", tmp_path, _e
                )


# ── Phase 2: Alpaca fills FIFO helpers ───────────────────────────────────────

def _fill_et_date(ts_str: str) -> str:
    """Convert Alpaca transaction_time UTC ISO timestamp to ET date string."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone(_ET).strftime("%Y-%m-%d")
    except Exception as _e:
        logger.warning(
            "[fill_date] Could not parse timestamp %r: %s — using raw slice", ts_str, _e
        )
        return str(ts_str)[:10] if ts_str and len(str(ts_str)) >= 10 else ""


def _fetch_alpaca_fills_for_date(date_str: str) -> list:
    """
    Fetch all FILL activities from Alpaca for a given ET calendar date.
    Paginates with after_id (not page_token — confirmed correct Apr 2026 audit).
    Retries 3x with exponential backoff on network error.
    Client-side filters to target ET date.
    """
    api_key    = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set in environment")

    et_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_ET)
    et_end   = datetime(et_start.year, et_start.month, et_start.day,
                        23, 59, 59, tzinfo=_ET)
    headers  = {
        "APCA-API-KEY-ID":     api_key,
        "APCA-API-SECRET-KEY": secret_key,
    }

    all_fills: list = []
    after_id: "str | None" = None

    while True:
        # Always anchor to ET day window; after_id added for pagination
        # (does not conflict)
        params = {
            "direction": "asc",
            "page_size": "100",
            "after":     et_start.isoformat(),
            "until":     et_end.isoformat(),
        }
        if after_id:
            params["after_id"] = after_id

        url      = (
            f"{_ALPACA_PAPER_BASE}/v2/account/activities/FILL?"
            + urllib.parse.urlencode(params)
        )
        last_exc = None

        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                    page_fills = json.loads(resp.read().decode())
                # Detect Alpaca error body (404/400 returns JSON with
                # "code", not an exception)
                if isinstance(page_fills, dict) and page_fills.get("code"):
                    raise RuntimeError(f"Alpaca FILL API error: {page_fills}")
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                _time_mod.sleep(2 ** attempt)

        if last_exc:
            raise last_exc

        if not page_fills:
            break

        all_fills.extend(page_fills)

        if len(page_fills) < 100:
            break  # last page

        after_id = page_fills[-1]["id"]

    # Client-side filter: keep only fills on the target ET date
    return [
        f for f in all_fills
        if _fill_et_date(f.get("transaction_time", "")) == date_str
    ]


def _fifo_reconstruct(fills: list, prior_lots: dict) -> tuple:
    """
    FIFO P&L reconstruction from Alpaca fills.

    prior_lots: {symbol: [{"qty": N, "price": P, "side": "long"|"short"}]}
                Open lots carried forward from the prior trading day.

    Handles mixed buy/buy_to_cover labels via net-position-aware matching:
    a "buy" that arrives when net position is short is treated as a cover.

    Returns:
        today_realized_pnl (float)
        remaining_lots     (dict)  — end-of-day open lot structure
        per_trade          (list)  — [{symbol, side, qty, entry, exit, pnl, filled_at}]
    """
    import copy
    lots      = copy.deepcopy(prior_lots)
    today_pnl = 0.0
    per_trade = []

    for fill in fills:
        sym       = fill.get("symbol", "")
        side      = fill.get("side", "")
        qty       = int(float(fill.get("qty", 0)))
        price     = float(fill.get("price", 0))
        filled_at = fill.get("transaction_time", "")

        if not sym or qty <= 0:
            continue

        if sym not in lots:
            lots[sym] = []

        current = lots[sym]
        net_qty = sum(
            lot["qty"] * (1 if lot["side"] == "long" else -1) for lot in current
        )

        if side in ("buy", "buy_to_cover"):
            if net_qty < 0:
                # Covering a short — match FIFO against short lots
                to_cover = qty
                while to_cover > 0 and current:
                    lot = current[0]
                    if lot["side"] != "short":
                        break
                    cover = min(lot["qty"], to_cover)
                    pnl   = (lot["price"] - price) * cover  # short: entry - exit
                    today_pnl += pnl
                    per_trade.append({
                        "symbol": sym, "side": "short",
                        "qty": cover, "entry": lot["price"],
                        "exit": price, "pnl": round(pnl, 4),  # 4dp storage
                        "filled_at": filled_at,
                    })
                    lot["qty"] -= cover
                    to_cover   -= cover
                    if lot["qty"] == 0:
                        current.pop(0)
                if to_cover > 0:
                    current.append({"qty": to_cover, "price": price, "side": "long"})
            else:
                current.append({"qty": qty, "price": price, "side": "long"})

        elif side in ("sell", "sell_short"):
            if net_qty > 0:
                # Closing a long — match FIFO against long lots
                to_sell = qty
                while to_sell > 0 and current:
                    lot = current[0]
                    if lot["side"] != "long":
                        break
                    sell = min(lot["qty"], to_sell)
                    pnl  = (price - lot["price"]) * sell  # long: exit - entry
                    today_pnl += pnl
                    per_trade.append({
                        "symbol": sym, "side": "long",
                        "qty": sell, "entry": lot["price"],
                        "exit": price, "pnl": round(pnl, 4),  # 4dp storage
                        "filled_at": filled_at,
                    })
                    lot["qty"] -= sell
                    to_sell    -= sell
                    if lot["qty"] == 0:
                        current.pop(0)
                if to_sell > 0:
                    current.append({"qty": to_sell, "price": price, "side": "short"})
            else:
                logger.critical(
                    "[FIFO] %s: closing sell with no open long lots "
                    "(net_qty=%d, qty=%d, price=%.2f). "
                    "Prior lots likely missing (bot restart / state corruption). "
                    "Recording as synthetic short — review FIFO state immediately.",
                    sym, net_qty, qty, price,
                )
                try:
                    from alerts import send_slack as _fifo_slack
                    _fifo_slack(
                        f"🚨 FIFO CRITICAL: {sym} closing sell with "
                        f"no prior long lots. Qty={qty} @ ${price:.2f}. "
                        f"Synthetic short recorded — verify positions."
                    )
                except Exception as _slack_err:
                    logger.warning("[FIFO] Slack alert failed: %s", _slack_err)
                current.append({"qty": qty, "price": price, "side": "short"})

    remaining_lots = {sym: lts for sym, lts in lots.items() if lts}
    return round(today_pnl, 2), remaining_lots, per_trade


def _load_prior_day_lots() -> dict:
    """Load open lot structure from end of the previous trading day."""
    try:
        if _LOTS_STATE_FILE.exists():
            with open(_LOTS_STATE_FILE) as f:
                data = json.load(f)
            stored_date = data.get("date", "")
            if stored_date:
                try:
                    stored_dt = datetime.strptime(stored_date, "%Y-%m-%d").date()
                    today_dt = datetime.now(_PT).date()
                    age_days = (today_dt - stored_dt).days
                    if age_days > 1:
                        logger.warning(
                            "[lots] Prior lots file is %d days old "
                            "(stored=%s, today=%s). "
                            "Loading anyway — verify open positions against Alpaca.",
                            age_days, stored_date, today_dt.isoformat(),
                        )
                except (ValueError, TypeError) as _de:
                    logger.warning(
                        "[lots] Cannot parse stored date %r: %s", stored_date, _de
                    )
            return data.get("open_lots", {})
    except Exception as exc:
        logger.warning(f"Could not load prior day lots state: {exc}")
    return {}


def _save_open_lots_state(lots: dict, date_str: str):
    """Persist end-of-day open lot structure for the next day's FIFO seeding."""
    try:
        _LOTS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(_LOTS_STATE_FILE, {"date": date_str, "open_lots": lots})
        logger.info(f"Open lots state saved for {date_str}: {len(lots)} symbol(s)")
    except Exception as exc:
        logger.warning(f"Could not save open lots state: {exc}")


class PortfolioTracker:
    def __init__(self):
        self.open_trades   = {}    # symbol -> trade dict
        self.closed_trades = []
        self.traded_today  = set()
        self._traded_today_date = datetime.now(_PT).strftime("%Y-%m-%d")
        self._day_trades   = []    # rolling PDT records — persisted to disk
        # RC-4: symbol → list of live trade refs
        self._unverified_exits: dict[str, list[dict]] = {}
        self._load_log()
        self._load_day_trades()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_log(self):
        if TRADE_LOG_FILE.exists():
            try:
                with open(TRADE_LOG_FILE) as f:
                    data = json.load(f)
                    _disk_closed = data.get("closed", [])
                    if len(_disk_closed) >= len(self.closed_trades):
                        self.closed_trades = _disk_closed
                    # safe: inside try — rebuilt immediately below
                    self._unverified_exits.clear()
                    # RC-4: rebuild unverified index on restart —
                    # survives os.execv nightly reset
                    for _t in self.closed_trades:
                        if (
                            _t.get("_fill_unverified")
                            and not _t.get("_patch_applied_ts")
                        ):
                            _sym = _t.get("symbol")
                            if _sym:
                                self._unverified_exits.setdefault(_sym, []).append(_t)
                    # Restore open trades if bot restarted mid-session
                    # S47 Bug3 guard (GAI): match on (symbol, entry_time), not
                    # symbol alone — same-day re-entry has a different entry_time
                    # and must NOT warn (symbol-only check causes false positives).
                    _closed_entry_keys = {
                        (t.get("symbol"), t.get("entry_time"))
                        for t in self.closed_trades
                    }
                    for t in data.get("open", []):
                        sym = t.get("symbol")
                        if sym:
                            if (sym, t.get("entry_time")) in _closed_entry_keys:
                                logger.warning(
                                    "[%s] _load_log: same entry_time in open "
                                    "AND closed — double-record (failed pop)? "
                                    "Keeping open. Verify reconcile_eod.py.",
                                    sym,
                                )
                            # S47f Phase 2a.5: Route externally-closed phantom to
                            # closed_trades. Prevents Day 2 entry rejection caused
                            # by a position that closed on Alpaca overnight but was
                            # never removed from open_trades (GTC stop while down).
                            if t.get("_fifo_reconciled_closed"):
                                _t_key = (sym, t.get("entry_time"))
                                if _t_key not in _closed_entry_keys:
                                    self.closed_trades.append(t)
                                    _closed_entry_keys.add(_t_key)
                                logger.info(
                                    "[%s] _load_log: _fifo_reconciled_closed=True"
                                    " — routed to closed_trades (external close,"
                                    " P&L needs manual verification).",
                                    sym,
                                )
                                continue
                            self.open_trades[sym] = t
            except Exception as e:
                logger.warning(f"Could not load trade log: {e}")

    def _save_log(self):
        _atomic_write(TRADE_LOG_FILE, {
            "open":         list(self.open_trades.values()),
            "closed":       self.closed_trades,
            "stats":        self.get_stats(),
            "last_updated": datetime.now(_PT).isoformat(),
        })

    def get_unverified_exits(self, max_age_minutes: int = 5) -> tuple:
        """RC-4: Return pending (symbol, trade_copy) pairs with unverified fills.

        Filters out entries older than max_age_minutes (expired) and entries
        already patched (_patch_applied_ts set). Writes back filtered lists to
        evict expired/patched entries from the secondary index (T1 fix).

        Returns:
            pending:  list[tuple[str, dict]] — (symbol, trade_copy) to reconcile
            expired:  list[str]  — symbols whose window elapsed (log + Slack)
        """
        now    = datetime.now(_PT)
        cutoff = now - timedelta(minutes=max_age_minutes)
        pending:        list[tuple[str, dict]] = []
        expired:        list[str]              = []
        syms_to_remove: list[str]              = []
        for sym, trade_list in self._unverified_exits.items():
            remaining_in_list: list[dict] = []
            for trade in trade_list:
                if not trade.get("_fill_unverified") or trade.get("_patch_applied_ts"):
                    continue
                _exit_str = trade.get("exit_time") or ""
                if not _exit_str:
                    continue
                try:
                    _exit_dt = datetime.fromisoformat(_exit_str)
                    if _exit_dt.tzinfo is None:
                        _exit_dt = _exit_dt.replace(tzinfo=_PT)
                except (ValueError, TypeError) as _e:
                    logger.warning(
                        "[%s] get_unverified_exits: unparseable exit_time"
                        " %r — skipping (%s)",
                        sym, _exit_str, _e,
                    )
                    continue
                if _exit_dt < cutoff:
                    if sym not in expired:
                        expired.append(sym)
                    continue
                remaining_in_list.append(trade)
                pending.append((sym, trade.copy()))
            # T1 fix: write back filtered list —
            # evicts expired/patched entries permanently
            self._unverified_exits[sym] = remaining_in_list
            if not remaining_in_list:
                syms_to_remove.append(sym)
        for sym in syms_to_remove:
            self._unverified_exits.pop(sym, None)
        return pending, expired

    def patch_exit_pnl(
        self,
        symbol: str,
        exit_price: float,
        fill_source: str = "reconciliation_pass",
        kelly=None,
        original_exit_time: str | None = None,
    ) -> bool:
        """RC-4: Overwrite exit_price + pnl on a fill_unverified closed trade.

        Searches closed_trades in reverse order (most recent first). Skips trades
        already patched (_patch_applied_ts set) and continues searching so that
        multiple unverified exits for the same symbol (re-entry same session) are
        each individually correctable (T6 fix).

        Uses _qty_at_close (pre-close remaining qty) to compute correct P&L for
        partial-exit trades where qty_remaining has already been zeroed (T1 fix).

        Updates pnl_pct using original full position qty (T7 fix).

        Returns True on success, False if no matching unverified trade found.
        """
        _trade: dict | None = None
        for _t in reversed(self.closed_trades):
            if _t.get("symbol") != symbol:
                continue
            if not _t.get("_fill_unverified"):
                continue
            if _t.get("_patch_applied_ts"):
                continue   # T6 fix: skip patched, keep searching for older unpatched
            if original_exit_time and _t.get("exit_time") != original_exit_time:
                continue
            _trade = _t
            break

        if _trade is None:
            logger.warning(
                f"[{symbol}] patch_exit_pnl: no _fill_unverified trade found"
            )
            return False

        if exit_price <= 0:
            logger.error(
                "[patch_exit_pnl] Invalid exit_price=%.4f for %s"
                " — skipping correction.",
                exit_price, symbol,
            )
            return False

        _original_exit_px = float(_trade.get("exit_price") or 0.0)
        _original_pnl     = float(_trade.get("pnl") or 0.0)
        _entry_px         = float(_trade.get("entry_price") or 0.0)

        # BUG-1 fix: 'or'-chain treats _qty_at_close=0 as falsy → falls back to
        # original qty. Use 'is not None' sentinel to distinguish legitimate zero
        # (fully partial-exited position) from absent field (legacy trade).
        # DS/GAI: add try/except + float() for type safety on non-numeric stored values.
        _qty_at_close_raw = _trade.get("_qty_at_close")
        try:
            if _qty_at_close_raw is not None and _qty_at_close_raw != "":
                _qty = int(float(_qty_at_close_raw))
            else:
                _qty = int(_trade.get("qty") or 0)
        except (ValueError, TypeError) as _qe:
            logger.warning(
                "[patch_exit_pnl] Non-numeric _qty_at_close %r for %s — "
                "falling back to original qty: %s",
                _qty_at_close_raw, symbol, _qe,
            )
            _qty = int(_trade.get("qty") or 0)
        if _qty < 0:
            logger.warning(
                "[patch_exit_pnl] Negative _qty=%d for %s — clamping to 0.",
                _qty, symbol,
            )
            _qty = 0

        _orig_qty_full = int(_trade.get("qty") or 0)
        _direction     = _trade.get("direction", "long")
        _dir_mult      = 1 if _direction == "long" else -1
        _partial_pnl   = float(_trade.get("partial_pnl") or 0.0)

        # GAI cross-bug guard: if _entry_px=0.0 (pending_overnight never promoted),
        # (exit_price - 0) * qty produces phantom profit equal to gross proceeds.
        # Preserve only partial_pnl; skip remaining-share P&L calculation.
        if _entry_px <= 0:
            logger.critical(
                "[patch_exit_pnl] _entry_px=%.4f is invalid for %s — "
                "cannot compute fill correction. Preserving partial_pnl=%.2f only.",
                _entry_px, symbol, _partial_pnl,
            )
            _new_pnl_remaining = 0.0
            _new_total_pnl     = round(_partial_pnl, 4)  # 4dp storage
        else:
            _new_pnl_remaining = round((exit_price - _entry_px) * _qty * _dir_mult, 4)
            _new_total_pnl     = round(_new_pnl_remaining + _partial_pnl, 4)  # 4dp

        _delay_secs = 0.0
        try:
            _exit_dt    = datetime.fromisoformat(_trade.get("exit_time") or "")
            _delay_secs = (datetime.now(_PT) - _exit_dt).total_seconds()
        except Exception as _e:
            logger.warning(
                "[patch_exit_pnl] Could not parse exit_time for %s: %s",
                symbol, _e,
            )

        _trade["exit_price"]         = exit_price
        _trade["pnl"]                = _new_total_pnl
        _trade["pnl_remaining"]      = _new_pnl_remaining
        _trade["_fill_unverified"]   = False
        _trade["_patch_applied_ts"]  = datetime.now(_PT).isoformat()
        _trade["_patch_fill_source"] = fill_source

        # T7 fix: update pnl_pct using original full qty (not zeroed qty_remaining)
        if _orig_qty_full > 0 and _entry_px > 0:
            _trade["pnl_pct"] = round(
                (_new_total_pnl / (_entry_px * _orig_qty_full)) * 100, 2
            )

        # Evict from secondary index — already patched, no longer pending
        if symbol in self._unverified_exits:
            self._unverified_exits[symbol] = [
                t for t in self._unverified_exits[symbol]
                if t.get("_fill_unverified") and not t.get("_patch_applied_ts")
            ]
            if not self._unverified_exits[symbol]:
                del self._unverified_exits[symbol]

        self._save_log()

        if kelly is not None:
            try:
                kelly.rebuild_from_trades(self.closed_trades)
            except Exception as _ke:
                logger.warning(
                    f"[{symbol}] patch_exit_pnl: Kelly rebuild failed: {_ke}"
                )

        try:
            _log_event(
                "exit_pnl_correction",
                symbol=symbol, price=exit_price, size=_qty,
                score=_trade.get("score", 0),
                mri_level=_trade.get("mri_level", "NORMAL"),
                data_source=fill_source,
                pdt_used=self.get_rolling_day_trade_count(),
                original_exit_price=_original_exit_px,
                corrected_exit_price=exit_price,
                original_pnl=_original_pnl,
                corrected_pnl=_new_total_pnl,
                correction_delay_secs=round(_delay_secs, 1),
            )
        except Exception as _le:
            logger.warning(
                f"[{symbol}] patch_exit_pnl: trade_events.jsonl write failed: {_le}"
            )

        logger.warning(
            f"[{symbol}] RC-4 fill_correction: "
            f"exit {_original_exit_px:.2f}→{exit_price:.2f} "
            f"| pnl {_original_pnl:.2f}→{_new_total_pnl:.2f} "
            f"| delay={_delay_secs:.0f}s | src={fill_source}"
        )
        return True

    def mark_fill_expired(self, symbol: str) -> bool:
        """RC-4 loop fix: mark expired-but-unverified exits to stop
        _load_log() re-queuing.
        Only marks trades older than 5 minutes (same cutoff as get_unverified_exits).
        Fresh trades (< 5 min old) are skipped — left for patch_exit_pnl to handle.
        """
        now    = datetime.now(_PT)
        cutoff = now - timedelta(minutes=5)
        found  = False
        for _t in reversed(self.closed_trades):
            if _t.get("symbol") != symbol:
                continue
            if not _t.get("_fill_unverified"):
                continue
            if _t.get("_patch_applied_ts"):
                continue
            _exit_str = _t.get("exit_time", "")
            try:
                _exit_dt = datetime.fromisoformat(_exit_str)
                if _exit_dt.tzinfo is None:
                    _exit_dt = _exit_dt.replace(tzinfo=_PT)
                else:
                    _exit_dt = _exit_dt.astimezone(_PT)
                if _exit_dt >= cutoff:
                    continue  # fresh trade — leave for patch_exit_pnl
            except Exception as _e:
                logger.warning(
                    "[%s] mark_fill_expired: unparseable exit_time %r — skipping (%s)",
                    symbol, _exit_str, _e,
                )
                continue
            _t["_patch_applied_ts"]       = now.isoformat()
            _t["_fill_reconcile_expired"] = True
            found = True
        if found:
            self._save_log()
            logger.warning(
                f"[{symbol}] RC-4 fill_expired: _patch_applied_ts set — "
                f"will not re-queue on next restart"
            )
        return found

    def _load_day_trades(self):
        """Load persisted day trades from disk. Called at startup.

        S47 Bug8: removed TOCTOU exists()→open() race (GAI); added 100ms retry
        for transient write-lock collisions (DS+GAI); CRITICAL+Slack on genuine
        corruption; self._day_trades=[] set before Slack call (DS).
        """
        for _attempt in range(2):
            try:
                with open(DAY_TRADES_FILE) as f:
                    self._day_trades = json.load(f)
                count = self.get_rolling_day_trade_count()
                logger.info(
                    f"PDT counter loaded from disk: {count} day trades"
                    f" in rolling window "
                    f"({len(self._day_trades)} total records)"
                )
                return
            except FileNotFoundError:
                self._day_trades = []
                return  # fresh install — correct, no alert needed
            except Exception as e:
                if _attempt == 0:
                    _time_mod.sleep(0.1)   # 100ms retry: transient write-lock collision
                    continue
                # Second attempt failed — file is genuinely corrupt
                self._day_trades = []   # set first: state consistent before Slack
                logger.critical(
                    "PDT day_trades file corrupt or unreadable after retry: %s — "
                    "resetting PDT counter to empty. "
                    "PDT state is UNKNOWN. Verify manually before next trade.",
                    e,
                )
                try:
                    from alerts import send_slack as _dt_slack
                    _dt_slack(
                        f":rotating_light: CRITICAL: PDT day_trades file unreadable "
                        f"({e}). PDT counter reset to empty. "
                        f"PDT state UNKNOWN — verify manually."
                    )
                except Exception as _dt_slack_err:
                    logger.warning(
                        "PDT corruption Slack alert failed: %s", _dt_slack_err
                    )

    def _save_day_trades(self):
        """Atomically save day trades to disk."""
        # Prune entries older than 7 business days to keep file small
        cutoff = datetime.now(_PT).date() - timedelta(days=10)
        self._day_trades = [
            t for t in self._day_trades
            if t.get("date", "1970-01-01") >= cutoff.isoformat()
        ]
        _atomic_write(DAY_TRADES_FILE, self._day_trades)

    # ── EOD summary ───────────────────────────────────────────────────────────

    def write_eod_summary(self, kelly_sizer=None):
        """
        Write a daily performance snapshot to logs/eod_YYYY-MM-DD.json.
        Called at EOD. Each file = one trading day. Feeds weekly_review.html.

        Phase 2: Alpaca fills FIFO is the authoritative source for pnl_today
        and all_time_stats.total_pnl. Tracker math runs in parallel for drift
        monitoring. Falls back to tracker on API failure (CRITICAL log + Slack).
        Dual-compute runs for 10 trading days (Phase 2a validation gate per Shaw).
        """
        today = datetime.now(_PT).strftime("%Y-%m-%d")
        eod_path = _ROOT / "logs" / f"eod_{today}.json"
        # Finding 6: anchor to project root, not CWD
        (_ROOT / "logs").mkdir(exist_ok=True)
        self._load_log()  # force reload — captures orphan/SIGKILL-reconciled exits

        today_trades = [
            t for t in self.closed_trades
            if t.get("exit_time", "").startswith(today)
        ]

        # PNL-DAYFIX: single source of truth for "what portion of this trade's P&L
        # belongs to today."  For overnight positions whose partial exit fired BEFORE
        # today, use only pnl_remaining to avoid double-counting the prior-day partial.
        # All other trades (same-day entries, same-day partials) use the full t["pnl"].
        # Called by today_pnls, score_buckets, and score_16pt_buckets —
        # must stay in sync.
        def _day_slice(t):
            _entry_date      = (t.get("entry_time") or "")[:10]
            _partial_exit_dt = (t.get("partial_exit_time") or "")[:10]
            _partial_pnl     = t.get("partial_pnl") or 0.0
            _pnl_rem         = t.get("pnl_remaining")
            if (
                _entry_date and _entry_date < today
                and abs(_partial_pnl) > 1e-8  # float exact-zero guard
                and _partial_exit_dt
                and _partial_exit_dt < today
                and _pnl_rem is not None
            ):
                return _pnl_rem
            return t.get("pnl", 0)

        today_pnls = [_day_slice(t) for t in today_trades]

        # Include partial_pnl from open positions whose last partial exit was today.
        # Partial exits are realized gains but the position stays in open_trades until
        # fully closed — they never appear in closed_trades / pnl_today otherwise.
        partial_realized_today = sum(
            float(t.get("partial_pnl", 0) or 0)
            for t in self.open_trades.values()
            if t.get("partial_exited")
            and (t.get("partial_exit_time") or "")[:10] == today
        )

        score_buckets = {}
        for t in today_trades:
            _sc = t.get('score', 0)
            bucket = f"{(_sc // 2) * 2}-{(_sc // 2) * 2 + 1}"
            if bucket not in score_buckets:
                score_buckets[bucket] = {"wins": 0, "losses": 0, "pnl": 0}
            _s = _day_slice(t)
            if _s > 0:
                score_buckets[bucket]["wins"] += 1
            else:
                score_buckets[bucket]["losses"] += 1
            score_buckets[bucket]["pnl"] = round(score_buckets[bucket]["pnl"] + _s, 2)

        # ── Tracker P&L (internal math — dual-compute cross-check) ──────────────
        _tracker_pnl = round(sum(today_pnls) + partial_realized_today, 2)

        # Consistency guard: score_bucket P&L sum must equal today_pnls sum.
        # A nonzero delta means a new P&L consumer forgot to call _day_slice().
        _bucket_sum = round(sum(v["pnl"] for v in score_buckets.values()), 2)
        _slice_sum  = round(sum(today_pnls), 2)
        if score_buckets and abs(_bucket_sum - _slice_sum) > 0.02:
            logger.warning(
                f"EOD INTERNAL DRIFT: score_buckets sum ${_bucket_sum:.2f} != "
                f"today_pnls sum ${_slice_sum:.2f} — check _day_slice() coverage"
            )

        # ── Phase 2a: Alpaca fills FIFO (authoritative source) ────────────────
        _alpaca_pnl       = None
        _alpaca_lots      = {}
        _alpaca_per_trade = []
        _alpaca_cumulative = None

        try:
            _prior_lots       = _load_prior_day_lots()
            _day_fills        = _fetch_alpaca_fills_for_date(today)

            # ── FIFO Orphan Lot Seeding ──────────────────────────────────────
            # Seed prior lots for positions whose opening fills are absent from
            # today's Alpaca fills (externally purchased before bot tracking).
            # Without seeding, FIFO treats closing sells as new short openings
            # → P&L = $0.
            #
            # Guard 1: skip if symbol already in prior_lots (normal carry).
            # Guard 2: skip if Alpaca has an opening fill today — intraday
            #           trades are handled natively by FIFO.
            # Guard 3: accumulate ALL closing fills before seeding
            #           (partial-fill sequence safety).
            # Guard 4: direction-aware — only accumulate fills that close
            #           the tracked side.
            _long_opened_today  = {
                f["symbol"] for f in _day_fills if f.get("side") == "buy"
            }
            _short_opened_today = {
                f["symbol"] for f in _day_fills
                if f.get("side") == "sell_short"
            }
            _orphan_seed_map: dict = {}
            for _fill in _day_fills:
                _fsym  = _fill.get("symbol", "")
                _fside = _fill.get("side", "")
                if not _fsym or _fsym in _prior_lots:
                    continue
                _fqty = int(float(_fill.get("qty") or 0))
                if _fqty <= 0:
                    continue
                if _fsym not in _orphan_seed_map:
                    _tr = next(
                        (t for t in today_trades if t.get("symbol") == _fsym),
                        None,
                    ) or next(
                        (v for v in self.open_trades.values()
                         if v.get("symbol") == _fsym),
                        None,
                    )
                    if _tr is None:
                        logger.warning(
                            f"FIFO orphan: closing fill for {_fsym} has no "
                            f"prior lot and no tracker record — fill unmatched."
                        )
                        _orphan_seed_map[_fsym] = None  # sentinel
                        continue
                    _entry = float(_tr.get("entry_price") or 0.0)
                    if _entry <= 0:
                        logger.warning(
                            f"FIFO orphan: {_fsym} entry_price not confirmed"
                            f" — deferring seed."
                        )
                        _orphan_seed_map[_fsym] = None  # sentinel
                        continue
                    _dir = _tr.get("direction", "long")
                    # Guard 2: intraday — opening fill exists today
                    if ((_dir == "long" and _fsym in _long_opened_today) or
                            (_dir == "short" and _fsym in _short_opened_today)):
                        _orphan_seed_map[_fsym] = None  # sentinel: intraday
                        continue
                    _orphan_seed_map[_fsym] = {
                        "qty": 0, "direction": _dir, "entry": _entry,
                    }
                _seed = _orphan_seed_map.get(_fsym)
                if _seed is None:
                    continue  # sentinel — skip all fills for this symbol
                _dir = _seed["direction"]
                # Guard 4: only closing fills for the tracked direction
                _is_closing = (
                    (_dir == "long"
                     and _fside in {"sell", "sell_short"}) or
                    (_dir == "short"
                     and _fside in {"buy", "buy_to_cover"})
                )
                if _is_closing:
                    _seed["qty"] += _fqty
            for _fsym, _seed in _orphan_seed_map.items():
                if _seed is None or _seed["qty"] <= 0:
                    continue
                _prior_lots[_fsym] = [{
                    "qty":   _seed["qty"],
                    "price": _seed["entry"],
                    "side":  _seed["direction"],
                }]
                logger.info(
                    f"FIFO orphan lot seeded: {_fsym} {_seed['qty']}sh "
                    f"@ ${_seed['entry']:.2f} ({_seed['direction']}) — "
                    f"no prior lot found (orphan/legacy entry)"
                )
            # ── end orphan seeding ───────────────────────────────────────────

            _alpaca_pnl, _alpaca_lots, _alpaca_per_trade = _fifo_reconstruct(
                _day_fills, _prior_lots
            )
            _save_open_lots_state(_alpaca_lots, today)

            # Cumulative total = prior EOD total + today's Alpaca P&L
            for _back in range(1, 8):
                _prev_date = (
                    datetime.strptime(today, "%Y-%m-%d") - timedelta(days=_back)
                ).strftime("%Y-%m-%d")
                _prev_eod_path = _ROOT / "logs" / f"eod_{_prev_date}.json"
                if _prev_eod_path.exists():
                    try:
                        with open(_prev_eod_path) as _f:
                            _prev_data = json.load(_f)
                        _prev_total = float(
                            _prev_data.get("all_time_stats", {}).get("total_pnl") or 0.0
                        )
                        _alpaca_cumulative = round(_prev_total + _alpaca_pnl, 2)
                    except Exception as _load_err:
                        logger.warning(
                            f"Failed to load prior EOD file {_prev_eod_path} — "
                            f"cumulative P&L will default to today only: {_load_err}"
                        )
                    break
            if _alpaca_cumulative is None:
                _alpaca_cumulative = _alpaca_pnl  # first trading day

            logger.info(
                f"Alpaca FIFO EOD: pnl_today=${_alpaca_pnl:.2f} | "
                f"cumulative=${_alpaca_cumulative:.2f} | "
                f"tracker=${_tracker_pnl:.2f} | "
                f"fills={len(_day_fills)} lots_closed={len(_alpaca_per_trade)}"
            )
        except Exception as _fifo_err:
            logger.critical(
                f"Alpaca fills FIFO failed — falling back to tracker P&L: {_fifo_err}"
            )
            try:
                from alerts import send_slack as _sl_fifo
                _sl_fifo(
                    ":rotating_light: *EOD P&L: Alpaca FIFO failed"
                    " — using tracker math*\n"
                    f"Error: `{str(_fifo_err)[:200]}`\n"
                    f"Tracker P&L: ${_tracker_pnl:.2f}. Verify fills manually."
                )
            except Exception as _slack_err:
                logger.warning(
                    f"Slack alert for FIFO failure could not be sent: {_slack_err}"
                )

        # Authoritative P&L — Alpaca wins; tracker is fallback.
        # A-4 paper-API gap: Alpaca paper fills endpoint sometimes returns 0 fills for
        # same-day trades (settlement delay). If Alpaca returned 0.0 with zero fills but
        # we have confirmed closed trades in the tracker, fall back to tracker
        # and log it.
        _a4_gap = (
            _alpaca_pnl is not None
            and _alpaca_pnl == 0.0
            and len(_day_fills) == 0
            and len(today_trades) > 0
        )
        if _a4_gap:
            logger.warning(
                f"A-4 paper fills gap: Alpaca returned 0 fills despite"
                f" {len(today_trades)} "
                f"closed trade(s) — using tracker P&L"
                f" (${_tracker_pnl:.2f}) as fallback. "
                f"Verify fills manually."
            )
        _pnl_today = (
            _tracker_pnl if _a4_gap
            else (_alpaca_pnl if _alpaca_pnl is not None else _tracker_pnl)
        )
        _pnl_drift = (
            round(_alpaca_pnl - _tracker_pnl, 2) if _alpaca_pnl is not None else None
        )

        # Drift alert (Majors requirement: >$1.00 triggers Slack WARNING)
        # A-4 gap: suppress — we already chose tracker fallback and logged the reason.
        if _pnl_drift is not None and abs(_pnl_drift) > 1.00 and not _a4_gap:
            logger.warning(
                f"EOD P&L DRIFT: Alpaca=${_alpaca_pnl:.2f} "
                f"tracker=${_tracker_pnl:.2f} drift=${_pnl_drift:+.2f}"
            )
            global _last_eod_drift_alert_date
            _today_dedup = datetime.now(_PT).strftime("%Y-%m-%d")
            if _last_eod_drift_alert_date != _today_dedup:
                _last_eod_drift_alert_date = _today_dedup
                _atomic_write(_DRIFT_ALERT_FILE, {"date": _today_dedup})
                try:
                    from alerts import send_slack as _sl_drift
                    _sl_drift(
                        f":warning: *EOD P&L drift detected (>$1.00 threshold)*\n"
                        f"Alpaca FIFO: ${_alpaca_pnl:.2f}"
                        f" | Tracker: ${_tracker_pnl:.2f} | "
                        f"Drift: ${_pnl_drift:+.2f}\n"
                        f"EOD file uses Alpaca as authoritative source."
                    )
                except Exception as _slack_err:
                    logger.warning(f"Slack drift alert could not be sent: {_slack_err}")

        # Build corrected all_time_stats — override total_pnl with Alpaca cumulative
        _stats = self.get_stats()
        if _alpaca_cumulative is not None:
            _stats["total_pnl"] = _alpaca_cumulative

        summary = {
            "date":           today,
            "trades_today":   len(today_trades),
            "pnl_today":      _pnl_today,
            "win_rate_today": round(
                len([p for p in today_pnls if p > 0]) / len(today_pnls) * 100, 1
            ) if today_pnls else 0,
            "score_buckets":  score_buckets,
            "all_time_stats": _stats,
            "trades":         today_trades,
            # Phase 2a dual-compute fields (10-day validation window per Shaw)
            "alpaca_pnl":      _alpaca_pnl,
            "tracker_pnl":     _tracker_pnl,
            "pnl_drift":       _pnl_drift,
            "alpaca_per_trade": _alpaca_per_trade,
        }

        # 16pt score bucket analysis
        score_16pt_buckets = {}
        for t in today_trades:
            s16 = t.get("score_16pt")
            if s16 is not None:
                bucket = f"{(s16 // 2) * 2}-{(s16 // 2) * 2 + 1}"
                if bucket not in score_16pt_buckets:
                    score_16pt_buckets[bucket] = {"wins": 0, "losses": 0, "pnl": 0}
                _s16 = _day_slice(t)
                if _s16 > 0:
                    score_16pt_buckets[bucket]["wins"] += 1
                else:
                    score_16pt_buckets[bucket]["losses"] += 1
                score_16pt_buckets[bucket]["pnl"] = round(
                    score_16pt_buckets[bucket]["pnl"] + _s16, 2
                )
        summary["score_16pt_buckets"] = score_16pt_buckets

        # Score comparison log
        # Schema: {"date": str, "scan_time": str, "trade_mode": str, "universe": str,
        #          "tickers": [{"symbol": str, "long_12pt": int, "short_12pt": int,
        #                       "long_16pt": int, "short_16pt": int, ...}, ...]}
        # Written by scan_to_html.py at EOD. avg_12pt/16pt = mean of max(long, short)
        # per ticker (best directional score — McKinney: correct aggregation
        # for mixed scanner).
        score_cmp_path = _ROOT / "logs" / f"score_comparison_{today}.json"
        if score_cmp_path.exists():
            try:
                with open(score_cmp_path) as _f:
                    cmp_data = json.load(_f)
                if isinstance(cmp_data, dict):
                    # Current format (S28+): dict wrapper with per-symbol tickers list
                    tickers = cmp_data.get("tickers")
                    if isinstance(tickers, list) and tickers:
                        avg_12 = sum(
                            max(
                                int(e.get("long_12pt") or 0),
                                int(e.get("short_12pt") or 0),
                            )
                            for e in tickers
                        ) / len(tickers)
                        avg_16 = sum(
                            max(
                                int(e.get("long_16pt") or 0),
                                int(e.get("short_16pt") or 0),
                            )
                            for e in tickers
                        ) / len(tickers)
                        summary["score_comparison_summary"] = {
                            "scans_logged": len(tickers),
                            "avg_12pt":     round(avg_12, 2),
                            "avg_16pt":     round(avg_16, 2),
                            "scan_date":    cmp_data.get("date", today),
                            "trade_mode":   cmp_data.get("trade_mode", ""),
                        }
                        logger.info(
                            "EOD score_comparison: %d tickers | avg_12pt=%.2f"
                            " | avg_16pt=%.2f",
                            len(tickers), avg_12, avg_16,
                        )
                    # Empty tickers list → scanner ran but found no candidates; silent
                elif isinstance(cmp_data, list) and cmp_data:
                    # Legacy format (pre-S28): list of {score_12pt, score_16pt}
                    avg_12 = sum(
                        e.get("score_12pt", 0) for e in cmp_data
                    ) / len(cmp_data)
                    avg_16 = sum(
                        e.get("score_16pt", 0) for e in cmp_data
                    ) / len(cmp_data)
                    summary["score_comparison_summary"] = {
                        "scans_logged": len(cmp_data),
                        "avg_12pt":     round(avg_12, 2),
                        "avg_16pt":     round(avg_16, 2),
                    }
                elif cmp_data is not None:
                    logger.warning(
                        "write_eod_summary: score_comparison unexpected"
                        " type %s — skipped",
                        type(cmp_data).__name__,
                    )
            except Exception as _e:
                logger.warning(
                    "write_eod_summary: score_comparison load failed"
                    " (%s) — skipped",
                    _e,
                )

        summary["signals_skipped"]  = getattr(self, "_signals_skipped_today", [])
        # Exclude SYNC (reconciliation markers) from display —
        # they don't consume PDT slots.
        summary["pdt_slots_used"]   = [
            t for t in self._day_trades if t.get("symbol") != "SYNC"
        ]
        # ── S47f Phase 2a.5: FIFO-driven overnight reconciliation ────────────────
        # Detects overnight positions that closed on Alpaca while the bot was down
        # (e.g. GTC stop fired) by comparing self.open_trades against _alpaca_lots
        # (FIFO remaining lots). Any overnight-flagged symbol absent from _alpaca_lots
        # has fully closed; we reconcile via record_exit() so it is correctly absent
        # from overnight_holds below.
        # Guards: _alpaca_pnl is not None (FIFO call succeeded); not _a4_gap (no
        # paper-API 0-fill anomaly that would make _alpaca_lots unreliable).
        # Q5: explicit catastrophe guard when both data structures are empty despite
        #     overnight positions (upstream silent FIFO failure).
        # Q4 (partial-close qty guard) deferred to P2 — board 3/4 OPTION B.
        if _alpaca_pnl is not None and not _a4_gap:
            _has_overnight = any(
                t.get("overnight") for t in self.open_trades.values()
            )
            if _has_overnight and not _alpaca_lots and not _alpaca_per_trade:
                # Q5 catastrophe guard: both data structures empty despite overnight
                # positions — likely upstream FIFO failure that didn't raise an
                # exception. Skip reconciliation to prevent spurious closure of all
                # open positions. EOD summary is written normally.
                logger.critical(
                    "write_eod_summary: Phase 2a.5 SKIPPED — overnight positions"
                    " exist in bot state but _alpaca_lots and _alpaca_per_trade"
                    " are both empty. Likely upstream data failure."
                    " Manual reconciliation required.",
                )
            else:
                for _sym_r in list(self.open_trades.keys()):
                    _tr_r = self.open_trades.get(_sym_r)
                    if not _tr_r or not _tr_r.get("overnight"):
                        continue
                    if _sym_r in _alpaca_lots:
                        continue  # Legitimately still open — FIFO lots remain
                    _fifo_matches = [
                        _pt for _pt in _alpaca_per_trade
                        if _pt.get("symbol") == _sym_r
                    ]
                    if _fifo_matches:
                        # DS P1: VWAP exit price across all per_trade lots (not [-1])
                        # DS P0: $0 guard — if all exit prices are 0.0, fall back
                        #         to marker only (no record_exit call).
                        _fifo_exits = [
                            float(pt.get("exit", 0.0))
                            for pt in _fifo_matches
                            if float(pt.get("exit", 0.0)) > 0.0
                        ]
                        _fifo_qtys = [
                            float(pt.get("qty", 0.0))
                            for pt in _fifo_matches
                            if float(pt.get("exit", 0.0)) > 0.0
                        ]
                        if not _fifo_exits:
                            # DS P0: no valid exit prices — marker-only path
                            if not _tr_r.get("_fifo_reconciled_closed"):
                                self.open_trades[_sym_r][
                                    "_fifo_reconciled_closed"
                                ] = True
                                self._save_log()
                                logger.error(
                                    "write_eod_summary: %s has no valid FIFO exit"
                                    " prices (all 0.0) — marked"
                                    " _fifo_reconciled_closed."
                                    " Manual P&L reconciliation required.",
                                    _sym_r,
                                )
                            continue
                        _fifo_zip = zip(
                            _fifo_exits, _fifo_qtys, strict=False
                        )
                        _fifo_exit_px = (
                            sum(e * q for e, q in _fifo_zip)
                            / sum(_fifo_qtys)
                        )
                        logger.warning(
                            "write_eod_summary: %s (dir=%s, overnight_since=%s)"
                            " fully closed on Alpaca — no remaining FIFO lots."
                            " Reconciling via record_exit(reason=external_close,"
                            " exit=%.4f).",
                            _sym_r,
                            _tr_r.get("direction", "?"),
                            _tr_r.get("overnight_since", "?"),
                            _fifo_exit_px,
                        )
                        try:
                            self.record_exit(
                                _sym_r,
                                exit_price=_fifo_exit_px,
                                reason="external_close",
                                mri_level=_tr_r.get("mri_level", "NORMAL"),
                            )
                            # Q1: correct exit_time to actual Alpaca fill timestamp
                            # so this reconciled trade does not appear in today_trades
                            # on any future write_eod_summary() call (today_trades
                            # filters by exit_time.startswith(today)).
                            # Q3: flag MRI level as uncertain for downstream analytics.
                            _actual_exit_ts = max(
                                (
                                    pt.get("filled_at") or ""
                                    for pt in _fifo_matches
                                    if pt.get("filled_at")
                                ),
                                default=None,
                            )
                            for _ct in reversed(self.closed_trades):
                                if (
                                    _ct.get("symbol") == _sym_r
                                    and not _ct.get("_recon_exit_ts_set")
                                ):
                                    if _actual_exit_ts:
                                        _ct["exit_time"] = _actual_exit_ts
                                    _ct["mri_at_exit_uncertain"] = True
                                    _ct["_recon_exit_ts_set"] = True
                                    break
                        except Exception as _recon_err:  # DS P1: try-except guard
                            if not self.open_trades.get(_sym_r, {}).get(
                                "_fifo_reconciled_closed"
                            ):
                                if _sym_r in self.open_trades:
                                    self.open_trades[_sym_r][
                                        "_fifo_reconciled_closed"
                                    ] = True
                                    self._save_log()
                            logger.error(
                                "write_eod_summary: record_exit() failed for %s"
                                " during FIFO reconciliation — marked"
                                " _fifo_reconciled_closed: %s",
                                _sym_r,
                                _recon_err,
                            )
                    else:
                        # No FIFO match — multi-day gap or fills absent from API.
                        if not _tr_r.get("_fifo_reconciled_closed"):
                            self.open_trades[_sym_r][
                                "_fifo_reconciled_closed"
                            ] = True
                            self._save_log()
                            logger.warning(
                                "write_eod_summary: %s has no remaining FIFO lots"
                                " and no per_trade entry — marked"
                                " _fifo_reconciled_closed and persisted.",
                                _sym_r,
                            )
        # ── end Phase 2a.5 ──────────────────────────────────────────────────────
        summary["overnight_holds"]  = [
            {
                "symbol":          s,
                "overnight_since": t.get("overnight_since", ""),
                "direction":       t.get("direction", ""),
            }
            for s, t in self.open_trades.items() if t.get("overnight")
        ]
        summary["exit_reasons"] = {}
        for t in today_trades:
            reason = t.get("exit_reason", "unknown")
            summary["exit_reasons"][reason] = summary["exit_reasons"].get(reason, 0) + 1

        _atomic_write(eod_path, summary)
        logger.info(f"EOD summary written → {eod_path}")

        # Rebuild Kelly stats from closed trades after FIFO (Thorp: Phase 2 deliverable)
        if kelly_sizer is not None:
            try:
                kelly_sizer.rebuild_from_trades(self.closed_trades)
                logger.info(
                    "Kelly stats rebuilt from corrected closed_trades post-EOD FIFO"
                )
            except Exception as _ke:
                logger.warning(f"Kelly rebuild after EOD FIFO failed: {_ke}")

        return summary

    # ── Trade recording ───────────────────────────────────────────────────────

    def record_entry(
        self,
        symbol: str,
        direction: str,
        qty: int,
        entry_price: float,
        stop: float,
        target: float,
        trade_mode: str,
        score: int,
        atr_value: Optional[float] = None,
        score_16pt: Optional[int] = None,
        # Guardrail 7 context — passed from main.py where full context is available
        mri_level: str = "NORMAL",
        data_source: str = "alpaca_data",
        pdt_used: int = 0,
        **extra_log,
    ):
        self.open_trades[symbol] = {
            "symbol":                 symbol,
            "direction":              direction,
            "qty":                    qty,
            "qty_remaining":          qty,
            "entry_price":            entry_price,
            "stop":                   stop,
            # AB-6R: immutable — never overwritten by breakeven/trail moves
            "original_stop":          stop,
            "target":                 target,
            "trail_stop":             None,
            "trade_mode":             trade_mode,
            "score":                  score,
            "score_16pt":             score_16pt,
            "atr_value":              atr_value,
            "partial_exited":         False,
            "entry_time":             datetime.now(_PT).isoformat(),
            "status":                 "open",
            "reversal_scan_count":    0,
            "reversal_confirm_count": 0,
            "overnight":              False,
            "overnight_since":        None,
            # GTC stop order submitted to Alpaca for overnight protection.
            # Cancelled at market open (so it doesn't show on L2 during RTH).
            "gtc_stop_order_id":      None,
            # Logged when Bucket A same-day block fires on a stop breach
            "stop_breached":          False,
            "stop_breach_price":      None,
            # Change 1: GTC stop-market order submitted when PDT=3/3 confirmed
            # stop fires. Distinct from gtc_stop_order_id (overnight protection stop).
            "_gtc_stop_order_id":     None,
        }
        self.traded_today.add(symbol)
        self._save_log()
        logger.info(
            f"[{symbol}] Entry recorded: {direction} {qty} @ ${entry_price:.2f}"
        )
        _log_event(
            "entry", symbol=symbol, price=entry_price, size=qty, score=score,
            mri_level=mri_level, data_source=data_source, pdt_used=pdt_used,
            direction=direction, stop=round(stop, 2), target=round(target, 2),
            trade_mode=trade_mode,
            **extra_log,
        )

    def set_gtc_stop_order_id(self, symbol: str, order_id: str):
        """Store the GTC stop order ID after submitting an overnight stop."""
        if symbol in self.open_trades:
            self.open_trades[symbol]["gtc_stop_order_id"] = order_id
            self._save_log()
            logger.debug(f"[{symbol}] GTC stop order ID stored: {order_id}")

    def set_pdt_gtc_stop_order_id(self, symbol: str, order_id: str):
        """Store the Change 1 PDT=3/3 confirmed-stop GTC order ID."""
        if symbol in self.open_trades:
            self.open_trades[symbol]["_gtc_stop_order_id"] = order_id
            self._save_log()
            logger.debug(f"[{symbol}] PDT GTC stop order ID stored: {order_id}")

    def clear_pdt_gtc_stop_order_id(self, symbol: str):
        """Clear the Change 1 PDT=3/3 GTC stop order ID after fill or cancel."""
        if symbol in self.open_trades:
            self.open_trades[symbol]["_gtc_stop_order_id"] = None
            self._save_log()

    def clear_gtc_stop_order_id(self, symbol: str):
        """Clear GTC stop order ID after cancellation at market open."""
        if symbol in self.open_trades:
            self.open_trades[symbol]["gtc_stop_order_id"] = None
            self._save_log()

    def get_overnight_gtc_positions(self) -> list:
        """
        Return list of (symbol, trade_dict) for all positions with an active GTC stop.
        Used at market open to cancel overnight stops before RTH begins.
        """
        return [
            (sym, trade)
            for sym, trade in self.open_trades.items()
            if trade.get("gtc_stop_order_id")
        ]

    # ── Overnight pending entry state machine ─────────────────────────────────

    def record_pending_entry(
        self,
        symbol: str,
        order_id: str,
        direction: str,
        qty: int,
        limit_price: float,
        stop: float,
        target: float,
        score: int,
        atr_value: Optional[float] = None,
    ):
        """
        Record a submitted overnight DAY limit order — awaiting fill.
        order_id is persisted FIRST (David Park: crash-safe ordering).
        Status: pending_overnight → open (on fill) or removed (on cancel/expire).
        """
        self.open_trades[symbol] = {
            "symbol":                 symbol,
            "order_id":               order_id,   # stored first — crash safety
            "direction":              direction,
            "qty":                    qty,
            "qty_remaining":          qty,
            "limit_price":            limit_price,
            "entry_price":            None,        # set on fill confirmation
            "stop":                   stop,
            "target":                 target,
            "trail_stop":             None,
            "trade_mode":             "intraday",
            "score":                  score,
            "atr_value":              atr_value,
            "partial_exited":         False,
            "entry_time":             None,        # set on fill confirmation
            "status":                 "pending_overnight",
            "reversal_scan_count":    0,
            "reversal_confirm_count": 0,
            "overnight":              True,
            "overnight_since":        datetime.now(_PT).isoformat(),
            "gtc_stop_order_id":      None,
            "stop_breached":          False,
            "stop_breach_price":      None,
            "submitted_at":           datetime.now(_PT).isoformat(),
        }
        self._save_log()
        logger.info(
            f"[{symbol}] Pending overnight entry recorded: {direction} {qty} "
            f"limit@${limit_price:.2f} | Order ID: {order_id}"
        )

    def promote_pending_to_active(
        self,
        symbol: str,
        fill_price: float,
        filled_qty: int = 0,
        mri_level: str = "NORMAL",
        pdt_used: int = 0,
    ):
        """Convert pending_overnight → open after Alpaca confirms fill."""
        if symbol not in self.open_trades:
            return
        t = self.open_trades[symbol]
        if t.get("status") != "pending_overnight":
            logger.warning(
                f"[{symbol}] promote_pending_to_active called on status="
                f"{t.get('status')!r} — skipping duplicate promotion"
            )
            return
        _qty = filled_qty if filled_qty > 0 else t.get("qty", 0)
        t["status"]      = "open"
        t["entry_price"] = fill_price
        t["entry_time"]  = datetime.now(_PT).isoformat()
        self.traded_today.add(symbol)
        self._save_log()
        logger.info(
            f"[{symbol}] Overnight limit FILLED @ ${fill_price:.2f}"
            " — position now active"
        )
        if _qty <= 0:
            logger.warning(
                f"[{symbol}] Entry event skipped: filled_qty={filled_qty} and "
                f"trade qty={t.get('qty', 0)} both zero — check trade state"
            )
            return
        try:
            _log_event(
                "entry",
                symbol      = symbol,
                score       = t.get("score", 0),
                mri_level   = mri_level,
                price       = fill_price,
                size        = _qty,
                pdt_used    = pdt_used,
                data_source = "overnight_limit_fill",
                trade_mode  = t.get("trade_mode", "intraday"),
                direction   = t.get("direction"),
                stop        = t.get("stop"),
                target      = t.get("target"),
            )
        except Exception as _log_err:
            logger.warning(
                f"[{symbol}] Overnight entry event logging failed: {_log_err}"
            )

    def cancel_pending_entry(self, symbol: str):
        """Remove a pending_overnight entry from tracker
        (order cancelled or expired)."""
        if (
            symbol in self.open_trades
            and self.open_trades[symbol].get("status") == "pending_overnight"
        ):
            del self.open_trades[symbol]
            self._save_log()
            logger.info(f"[{symbol}] Pending overnight entry removed from tracker")

    def get_pending_overnight_entries(self) -> list:
        """Return list of (symbol, trade_dict) for all pending_overnight entries."""
        return [
            (sym, trade)
            for sym, trade in self.open_trades.items()
            if trade.get("status") == "pending_overnight"
        ]

    def record_partial_exit(
        self,
        symbol: str,
        exit_price: float,
        qty_closed: int,
        trail_stop: float,
    ) -> float:
        """
        Record a partial exit (first target hit).
        Updates qty_remaining, activates trailing stop on the remainder.
        Returns realized P&L on the closed portion.
        """
        if symbol not in self.open_trades:
            return 0.0
        trade     = self.open_trades[symbol]
        # Idempotency guard: if same price+qty was already partially exited,
        # skip duplicate
        _existing_partial_price = trade.get("partial_exit_price")
        _existing_partial_qty   = trade.get("partial_exit_qty_last")
        if (
            _existing_partial_price == exit_price
            and _existing_partial_qty == qty_closed
        ):
            logger.warning(
                f"[{symbol}] record_partial_exit called with same price/qty"
                f" as last partial — skipping duplicate (idempotency guard)"
            )
            return 0.0
        trade["partial_exit_qty_last"] = qty_closed  # track for idempotency
        direction = trade["direction"]
        entry     = trade["entry_price"]

        pnl = (exit_price - entry) * qty_closed if direction == "long" \
              else (entry - exit_price) * qty_closed

        trade["qty_remaining"]    -= qty_closed
        # Guard: qty_remaining must be [0, qty] —
        # reconciliation can produce impossible states
        trade["qty_remaining"]     = max(0, min(trade["qty_remaining"], trade["qty"]))
        trade["partial_exited"]    = True
        trade["trail_stop"]        = trail_stop
        # breakeven floor after partial
        trade["stop"]              = trade.get("entry_price")
        trade["partial_exit_price"] = exit_price
        trade["partial_exit_time"]  = datetime.now(_PT).isoformat()
        # accumulate across all tranches
        # 4dp storage: prevents false-zero accumulation on tiny P&L
        trade["partial_pnl"]        = round(trade.get("partial_pnl", 0.0) + pnl, 4)

        self._save_log()
        logger.info(
            f"[{symbol}] Partial exit: {qty_closed} shares @ ${exit_price:.2f} "
            f"| P&L ${pnl:.2f} | {trade['qty_remaining']} remaining | "
            f"Trail stop: ${trail_stop:.2f}"
        )
        return pnl

    def update_trail_stop(self, symbol: str, new_trail_stop: float):
        """Ratchet the trailing stop in the favorable direction."""
        if symbol not in self.open_trades:
            return
        trade     = self.open_trades[symbol]
        old       = trade.get("trail_stop")
        direction = trade["direction"]
        if direction == "long" and (old is None or new_trail_stop > old):
            trade["trail_stop"] = new_trail_stop
        elif direction == "short" and (old is None or new_trail_stop < old):
            trade["trail_stop"] = new_trail_stop

    def record_exit(
        self, symbol: str, exit_price: float, reason: str = "signal",
        # BUG-E2E-4: was always "NORMAL" (hardcoded default in _log_event)
        mri_level: str = "NORMAL",
    ):
        # BV-1: normalize None/""/UNKNOWN to baseline
        # (MRI is background-only; absence = NORMAL)
        if mri_level in (None, "", "UNKNOWN"):
            mri_level = "NORMAL"
        if symbol not in self.open_trades:
            return
        # DS guard: reject already-closed trade in open_trades (corrupt restart state)
        if self.open_trades.get(symbol, {}).get("status") == "closed":
            logger.warning(
                f"[{symbol}] record_exit called on already-closed trade — skipping"
            )
            return
        # Validate original qty BEFORE pop — prevents orphaned trade on early return
        _original_qty = self.open_trades[symbol].get("qty", 0)
        if _original_qty <= 0:
            logger.error(
                f"[{symbol}] record_exit: invalid original qty"
                f" ({_original_qty}) — skipping"
            )
            return
        # BUG-5: validate entry_price BEFORE pop — if entry is None/0 the trade is
        # still closed (P&L=$0.00) but we need this check here to log CRITICAL + Slack
        # before pop so the warning is never lost if something goes wrong downstream.
        _raw_entry_chk = self.open_trades[symbol].get("entry_price")
        if _raw_entry_chk is None or float(_raw_entry_chk or 0.0) <= 0:
            logger.critical(
                "[%s] record_exit: entry_price=%r is None/zero — "
                "trade will be closed with P&L=$0.00. "
                "Check pending_overnight promotion and fill logs.",
                symbol, _raw_entry_chk,
            )
            try:
                from alerts import send_slack as _ep_slack
                _ep_slack(
                    f"🚨 CRITICAL: {symbol} closed with missing/zero entry_price. "
                    f"P&L forced to $0.00. Check OCI logs immediately."
                )
            except Exception as _ep_slack_err:
                logger.warning(
                    "[%s] entry_price Slack alert failed: %s", symbol, _ep_slack_err
                )
        trade     = self.open_trades.pop(symbol)
        # Clamp qty_remaining to [0, original_qty].
        # Trades opened before qty_remaining was added fall back to original qty
        # via default.
        # Never force 0 back to original_qty — 0 is valid after full partial exit (GAI).
        qty           = trade.get("qty_remaining", _original_qty)
        qty           = max(0, min(qty, _original_qty))
        _partial_pnl  = trade.get("partial_pnl", 0.0)
        entry         = float(trade.get("entry_price") or 0.0)
        direction     = trade["direction"]

        # qty=0 with no prior partial exits = reconciliation/restart corruption.
        # partial_exited flag (set by record_partial_exit L1170) is the reliable
        # discriminator — handles breakeven tranche where partial_pnl=0.
        if qty == 0 and not trade.get("partial_exited", False):
            _reason_str = str(reason) if reason else ""
            if _reason_str.startswith("external_close"):
                logger.warning(
                    "[%s] record_exit: external_close with zero qty and no partials — "
                    "likely already reconciled. Skipping fallback. "
                    "P&L set to $0.00. Review fill_reconciler/reconcile_eod logs.",
                    symbol,
                )
            elif entry > 0:
                logger.warning(
                    "[%s] record_exit: qty_remaining=0 with no partial P&L — "
                    "reconciliation artifact. Falling back to original qty %d. "
                    "Verify Alpaca fills; review fill_reconciler/reconcile_eod logs.",
                    symbol, _original_qty,
                )
                qty = _original_qty
            else:
                logger.error(
                    "[%s] record_exit: corrupt trade state — qty_remaining=0, "
                    "no partial exits, entry_price=%.2f (invalid). "
                    "Cannot fallback. P&L will be $0.00. Review logs.",
                    symbol, entry,
                )

        # BUG-5: if entry=0.0 (corrupt/never-promoted trade), force pnl=0.0.
        # Without this guard, pnl = (exit_price - 0.0) * qty = gross proceeds as profit.
        # S47 Bug2: set _fill_unverified=True so patch_exit_pnl() routes this to the
        # entry=0 guard at L564–574 (preserves partial_pnl only) and excludes this
        # trade from get_stats() denominators until reconciled (DS+GAI S47).
        if entry <= 0:
            pnl = 0.0
            trade["_fill_unverified"] = True
        else:
            pnl = (exit_price - entry) * qty if direction == "long" \
                  else (entry - exit_price) * qty

        # Include all locked-in partial exit P&L in the final trade record.
        # partial_pnl accumulates across T1/T2/T3 tranches in record_partial_exit().
        # Without this, partial profits are silently erased from win/loss stats,
        # daily P&L, and kill switch calculations.
        _total_pnl   = round(pnl + _partial_pnl, 4)  # 4dp storage

        # pnl_pct uses original full position value for accurate return measurement.
        # Use pre-validated _original_qty — avoids corrupt pnl_pct if
        # trade["qty"] missing.
        _orig_qty    = _original_qty
        trade.update({
            "exit_price":    exit_price,
            "exit_time":     datetime.now(_PT).isoformat(),
            "exit_reason":   reason,
            "pnl":           _total_pnl,
            # final-close portion only (for audit)
            "pnl_remaining": round(pnl, 4),  # 4dp storage
            "pnl_pct":       (
                round((_total_pnl / (entry * _orig_qty)) * 100, 2)
                if entry > 0 and _orig_qty > 0 else 0.0
            ),
            "status":        "closed",
            # BUG-1 fix: zero on close (was stale pre-exit value)
            "qty_remaining": 0,
            # RC-4: pre-close remaining qty for fill reconciliation
            "_qty_at_close": qty,
        })
        self.closed_trades.append(trade)
        # P2-MANUAL-AUDIT: write external closes to manual_audit.jsonl
        # for operator review
        if reason and str(reason).startswith("external_close"):
            _json_audit = json
            from zoneinfo import ZoneInfo as _ZI_audit
            _pt_now = datetime.now(_ZI_audit("America/Los_Angeles")).isoformat()
            _audit_record = {
                "ts_pt":       _pt_now,
                "event":       "external_close",
                "symbol":      symbol,
                "reason":      reason,
                "direction":   trade.get("direction"),
                "entry_price": trade.get("entry_price"),
                "exit_price":  exit_price,
                "qty":         trade.get("qty"),
                "pnl":         trade.get("pnl"),
                "score":       trade.get("score"),
            }
            try:
                _audit_path = (
                    Path(__file__).resolve().parent.parent
                    / "logs" / "manual_audit.jsonl"
                )
                with open(_audit_path, "a") as _af:
                    _af.write(_json_audit.dumps(_audit_record) + "\n")
            except Exception as _audit_err:
                logger.warning(
                    f"[{symbol}] manual_audit.jsonl write failed: {_audit_err}"
                )
        # RC-4: populate index BEFORE _save_log() — ordering guard (DS F9)
        if trade.get("_fill_unverified"):
            self._unverified_exits.setdefault(symbol, []).append(trade)
        self._save_log()
        logger.info(
            f"[{symbol}] Exit recorded: ${exit_price:.2f}"
            f" | P&L: ${_total_pnl:.2f} ({reason})"
        )
        # Guardrail 7: structured exit event — stop_hit or exit depending on reason
        _evt = (
            "stop_hit"
            if any(s in str(reason or "") for s in _LOG_STOP_REASONS)
            else "exit"
        )
        _log_event(
            _evt, symbol=symbol, price=exit_price,
            size=qty, score=trade.get("score", 0),
            pnl=_total_pnl, reason=reason, direction=direction,
            mri_level=mri_level,   # BUG-E2E-4: pass through caller-supplied MRI level
        )
        # Return _total_pnl (remaining close + all partial tranches) — not pnl alone.
        # Every caller feeds this into risk.register_close() → daily_pnl → kill switch.
        # Returning pnl (remaining only) made kill switch blind to partial exit profits.
        return _total_pnl

    def record_gtc_triggered(self, symbol: str, exit_price: float):
        """
        Record that an overnight GTC stop was triggered by Alpaca.
        Called at startup when reconciling GTC orders.
        Closes the tracker record with reason='gtc_stop_triggered'.
        """
        if symbol not in self.open_trades:
            logger.warning(
                f"[{symbol}] record_gtc_triggered called but symbol not in tracker"
            )
            return
        pnl = self.record_exit(symbol, exit_price, reason="gtc_stop_triggered")
        logger.info(
            f"[{symbol}] GTC stop triggered overnight — "
            f"tracker closed at ${exit_price:.2f} | P&L: ${pnl:.2f}"
        )
        return pnl

    def record_stop_breach_blocked(self, symbol: str, current_price: float):
        """
        Called when Bucket A same-day block fires AND the stop is breached.
        Logs the breach in the trade dict for EOD review.
        """
        if symbol not in self.open_trades:
            return
        trade = self.open_trades[symbol]
        active_stop = trade.get("trail_stop") or trade.get("stop")
        trade["stop_breached"]    = True
        trade["stop_breach_price"] = current_price
        self._save_log()
        logger.warning(
            f"[{symbol}] Bucket A STOP BREACH BLOCKED — "
            f"price ${current_price:.2f} vs stop ${active_stop:.2f} | "
            f"Position held per same-day hard rule. Review at EOD."
        )

    # ── Utility queries ───────────────────────────────────────────────────────

    def is_in_trade(self, symbol: str) -> bool:
        today = datetime.now(_PT).strftime("%Y-%m-%d")
        if today != self._traded_today_date:
            self.traded_today = set()
            self._traded_today_date = today
        return symbol in self.open_trades or symbol in self.traded_today

    def get_trade(self, symbol: str) -> dict:
        return self.open_trades.get(symbol)

    def opened_today(self, symbol: str) -> bool:
        """Return True if this symbol was opened in the current trading session."""
        trade = self.open_trades.get(symbol)
        if not trade:
            return False
        return (
            trade.get("entry_time", "")[:10] == datetime.now(_PT).strftime("%Y-%m-%d")
        )

    # ── PDT counter ───────────────────────────────────────────────────────────

    def record_day_trade(self, symbol: str):
        """
        Record a same-day round-trip (day trade) with timestamp.
        Persisted to logs/day_trades.json so it survives bot restarts.
        """
        self._day_trades.append({
            "symbol":    symbol,
            "timestamp": datetime.now(_PT).isoformat(),
            "date":      datetime.now(_PT).strftime("%Y-%m-%d"),
        })
        self._save_day_trades()
        logger.info(
            f"[{symbol}] Day trade recorded. "
            f"Rolling count: {self.get_rolling_day_trade_count()}/3"
        )

    @staticmethod
    def _market_holidays() -> set:
        """
        Return set of ISO date strings that are NYSE market holidays.
        Pulled from events/calendar.py BLACKOUT entries — single source of truth.
        Falls back to a hardcoded minimal set if the calendar cannot be imported.
        """
        global _market_holidays_fallback_logged
        try:
            from events.calendar import STATIC_EVENTS as EVENTS, EventRisk
            holidays = {
                e["date"] for e in EVENTS
                if e.get("risk") == EventRisk.BLACKOUT and "date" in e
            }
            if _market_holidays_fallback_logged:
                logger.info(
                    "_market_holidays: events.calendar import succeeded — "
                    "restored from hardcoded fallback."
                )
                _market_holidays_fallback_logged = False
            return holidays
        except Exception as _e:
            if not _market_holidays_fallback_logged:
                logger.critical(
                    "_market_holidays: events.calendar import failed (%s) — "
                    "falling back to hardcoded 2026 NYSE holidays. "
                    "PDT holiday counting is degraded. Investigate calendar.py import.",
                    _e,
                )
                _market_holidays_fallback_logged = True
            # NYSE-observed full-day market closures for 2026.
            # Early closes (half days) are NOT included — they remain trading days.
            return {
                "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
                "2026-05-25", "2026-06-19",  # Juneteenth — added 2026-05-06 S9
                "2026-07-03", "2026-09-07", "2026-11-26",
                "2026-12-25",
            }

    def get_rolling_day_trade_count(self) -> int:
        """
        Count day trades in the last 5 TRADING days (rolling window).
        Excludes weekends AND NYSE market holidays (Good Friday, etc.).
        Anchors on most recent trading day so weekend/holiday runs count correctly.
        Maps holiday/weekend-stamped EXTERNAL entries back to prior trading day
        (Alpaca reconciliation happens after close, sometimes stamped next day).
        """
        holidays = self._market_holidays()

        def _is_trading_day(d: date) -> bool:
            return d.weekday() < 5 and d.isoformat() not in holidays

        anchor = datetime.now(_PT).date()
        while not _is_trading_day(anchor):
            anchor -= timedelta(days=1)

        trading_days: list[str] = []
        d = anchor
        while len(trading_days) < 5:
            if _is_trading_day(d):
                trading_days.append(d.isoformat())
            d -= timedelta(days=1)

        def _eff(raw: str) -> str:
            """Map a date string back to the nearest prior trading day."""
            if not raw:
                return ""
            try:
                d2 = date.fromisoformat(raw)
                while not _is_trading_day(d2):
                    d2 -= timedelta(days=1)
                return d2.isoformat()
            except Exception as _e:
                logger.warning("_eff: date map failed for %r: %s", raw, _e)
                return raw

        return min(sum(1 for t in self._day_trades
                       if t.get("symbol") != "SYNC"
                       and _eff(t.get("date", "")) in trading_days), 3)

    def _real_rolling_count(self) -> int:
        """
        Count only bot-recorded (non-phantom) day trades in the rolling window.
        Excludes EXTERNAL and SYNC entries — used to determine if a downward
        PDT reconciliation is safe (i.e. excess is purely phantom noise).
        """
        holidays = self._market_holidays()

        def _is_td(d: date) -> bool:
            return d.weekday() < 5 and d.isoformat() not in holidays

        anchor = datetime.now(_PT).date()
        while not _is_td(anchor):
            anchor -= timedelta(days=1)

        tdays: list[str] = []
        d = anchor
        while len(tdays) < 5:
            if _is_td(d):
                tdays.append(d.isoformat())
            d -= timedelta(days=1)

        def _eff(raw: str) -> str:
            if not raw:
                return ""
            try:
                d2 = date.fromisoformat(raw)
                while not _is_td(d2):
                    d2 -= timedelta(days=1)
                return d2.isoformat()
            except Exception as _e:
                logger.warning("_eff: date map failed for %r: %s", raw, _e)
                return raw

        _phantom = {"EXTERNAL", "SYNC"}
        return min(sum(1 for t in self._day_trades
                       if t.get("symbol") not in _phantom
                       and _eff(t.get("date", "")) in tdays), 3)

    def sync_pdt_with_alpaca(self, alpaca_daytrade_count: int):
        """
        Compare our rolling day trade count with Alpaca's reported count.
        - Alpaca > local  → pad with EXTERNAL entries (existing behaviour)
        - Alpaca < local  → trim phantom (EXTERNAL/SYNC) entries if safe to do so;
                            never touch bot-recorded real trades
        - Alpaca == local → in sync, log info
        """
        our_count = self.get_rolling_day_trade_count()

        if alpaca_daytrade_count == our_count:
            logger.info(f"PDT counter in sync with Alpaca: {our_count}/3")
            return

        logger.warning(
            f"PDT count mismatch — Alpaca reports {alpaca_daytrade_count}, "
            f"tracker has {our_count} in rolling window. "
            f"Trades may have occurred outside this bot instance."
        )

        if alpaca_daytrade_count > our_count:
            # Alpaca knows about more day trades — pad with EXTERNAL entries
            gap = alpaca_daytrade_count - our_count
            for _ in range(gap):
                self._day_trades.append({
                    "symbol":    "EXTERNAL",
                    "timestamp": datetime.now(_PT).isoformat(),
                    "date":      datetime.now(_PT).strftime("%Y-%m-%d"),
                })
            self._save_day_trades()
            logger.warning(
                f"Added {gap} EXTERNAL entry/entries to PDT counter to match Alpaca."
            )

        else:
            # Alpaca has fewer — only safe to trim if excess is purely phantom entries
            real_count = self._real_rolling_count()
            if alpaca_daytrade_count >= real_count:
                # Excess is phantom noise — trim phantom entries from the tail.
                # Two-pass order: EXTERNAL first, SYNC second.
                # EXTERNAL entries ARE counted by get_rolling_day_trade_count(), so
                # removing them actually reduces the rolling count. SYNC entries are
                # already excluded from the count — removing SYNC alone is a no-op and
                # leaves the count artificially high (root cause of PDT lock bug).
                # Cap at 2 removals per sync call to prevent runaway
                # trimming (Finding 3)
                excess       = our_count - alpaca_daytrade_count
                _removal_cap = min(excess, 2)
                new_trades   = list(self._day_trades)
                removed      = 0
                _removed_syms = []
                for _priority in ("EXTERNAL", "SYNC"):
                    for i in range(len(new_trades) - 1, -1, -1):
                        if removed >= _removal_cap:
                            break
                        if new_trades[i].get("symbol") == _priority:
                            _removed_syms.append(_priority)
                            new_trades.pop(i)
                            removed += 1
                    if removed >= _removal_cap:
                        break
                self._day_trades = new_trades
                self._save_day_trades()
                logger.info(
                    f"PDT reconciled down: removed {removed} phantom entry/entries "
                    f"({', '.join(_removed_syms)}) "
                    f"(Alpaca={alpaca_daytrade_count}, was {our_count}). "
                    f"New rolling count: {self.get_rolling_day_trade_count()}/3"
                )
            else:
                # Alpaca < real bot trades — data integrity issue, keep local (safe)
                logger.critical(
                    f"PDT INTEGRITY: Alpaca reports {alpaca_daytrade_count} but bot "
                    f"recorded {real_count} real trades in rolling window. "
                    f"Manual review required. Keeping local count"
                    f" ({our_count}) for safety."
                )
                try:
                    from alerts import send_slack as _sl_pdt
                    _sl_pdt(
                        f":rotating_light: *PDT COUNT INTEGRITY MISMATCH*\n"
                        f"Alpaca reports {alpaca_daytrade_count} day trades, "
                        f"bot recorded {real_count} real trades in rolling window.\n"
                        f"Keeping local count ({our_count}/3). Manual review required."
                    )
                except Exception as _slack_err:
                    logger.warning(
                        f"Slack PDT mismatch alert could not be sent: {_slack_err}"
                    )

    # ── Stats ─────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        if not self.closed_trades:
            return {"total_trades": 0}
        import math
        import statistics as _stats

        # S47 Bug2: exclude _fill_unverified trades from stats denominators.
        # Unverified fills have pnl=$0.00 (forced by entry=None guard in record_exit
        # or by fetch_actual_fill_price fallback) — including them inflates total_trades
        # and skews win_rate/Sharpe without contributing real signal (DS+GAI S47).
        pnls   = [
            t["pnl"] for t in self.closed_trades
            if not t.get("_fill_unverified")
        ]
        wins   = [p for p in pnls if p > 0]
        # Exclude breakeven (pnl=0.0) from losses and the win_rate denominator.
        # DS+GAI consensus S20: scratches are not risk outcomes; including them
        # in losses deflates avg_loss and distorts the Kelly win rate input.
        losses = [p for p in pnls if p < 0]

        sharpe = 0
        if len(pnls) >= 2:
            mean_pnl = _stats.mean(pnls)
            std_pnl  = _stats.stdev(pnls)
            sharpe   = (
                round((mean_pnl / std_pnl) * math.sqrt(252), 3) if std_pnl > 0 else 0
            )

        sortino = 0
        downside = [p for p in pnls if p < 0]
        if len(downside) >= 2 and len(pnls) >= 2:
            mean_pnl     = _stats.mean(pnls)
            downside_std = _stats.stdev(downside)
            sortino      = round(
                (mean_pnl / downside_std) * math.sqrt(252), 3
            ) if downside_std > 0 else 0

        # S47 Bug1: count unverified before loop so it's available for return dict and
        # warning log even if r_multiples ends up empty.
        _unverified_count = sum(
            1 for t in self.closed_trades if t.get("_fill_unverified")
        )
        r_multiples = []
        for t in self.closed_trades:
            if t.get("_fill_unverified"):       # S47 Bug1: exclude — pnl unreliable
                continue
            entry         = t.get("entry_price") or 0
            # fallback for legacy trades
            original_stop = t.get("original_stop") or t.get("stop") or 0
            pnl           = t.get("pnl") or 0
            qty           = t.get("qty") or 1
            risk_per_share = abs(entry - original_stop)
            if risk_per_share <= 0 or qty <= 0:
                continue
            _r = pnl / (risk_per_share * qty)
            # ±50R clamp: prevents scratch-stop / corrupt-stop R explosion from
            # distorting avg_r_multiple (DS+GAI S47 — ±50R covers all realistic
            # paper-account outcomes; any |R|>50 is corrupt data, not a real trade).
            r_multiples.append(max(-50.0, min(50.0, _r)))

        if r_multiples:
            avg_r = round(sum(r_multiples) / len(r_multiples), 3)
        else:
            avg_r = 0
            if self.closed_trades:
                logger.warning(
                    "get_stats: no verified R-multiples (%d trades total, "
                    "%d unverified). avg_r_multiple=0 — Kelly will use "
                    "minimum sizing until verified trades accumulate.",
                    len(self.closed_trades), _unverified_count,
                )

        return {
            # S47 Bug2: total_trades = ALL closed (for audit); verified_trades and
            # unverified_trades let callers distinguish clean vs dirty subsets.
            "total_trades":     len(self.closed_trades),
            "verified_trades":  len(pnls),
            "unverified_trades": _unverified_count,
            "win_rate":       (
                round(len(wins) / (len(wins) + len(losses)) * 100, 1)
                if (wins or losses) else 0.0
            ),
            "total_pnl":      round(sum(pnls), 2),
            "avg_win":        round(sum(wins) / len(wins), 2) if wins else 0,
            "avg_loss":       round(sum(losses) / len(losses), 2) if losses else 0,
            "profit_factor":  round(
                sum(wins) / abs(sum(losses)), 2
            ) if losses else (0.0 if not wins else float("inf")),
            "sharpe_ratio":   sharpe,
            "sortino_ratio":  sortino,
            "avg_r_multiple": avg_r,
            "open_trades":    len(self.open_trades),
        }

    def attach_news_summary(self, news_summary: dict):
        self._news_summary = news_summary

    def get_news_summary(self) -> dict:
        return getattr(self, "_news_summary", {})

    def print_stats(self):
        stats = self.get_stats()
        print("\n" + "=" * 50)
        print("PORTFOLIO STATS")
        print("=" * 50)
        for k, v in stats.items():
            print(f"  {k:<20} {v}")
        print("=" * 50 + "\n")


# ── Module-level PDT helpers (importable by dashboard / weekly_review) ────────
# These are thin wrappers around PortfolioTracker's canonical logic so that
# generate_dashboard.py and weekly_review.py don't re-implement PDT counting.
# Single source of truth: PortfolioTracker._market_holidays() + rolling window.

def compute_rolling_pdt_count(day_trades: list) -> int:
    """
    Canonical rolling 5-trading-day PDT count — standalone for scripts that
    don't hold a PortfolioTracker instance (generate_dashboard, weekly_review).
    Excludes 'SYNC' (reconciliation marker). EXTERNAL counts as real PDT.
    Holiday-aware via PortfolioTracker._market_holidays(). Caps at 3.
    """
    holidays = PortfolioTracker._market_holidays()

    def _is_td(d: date) -> bool:
        return d.weekday() < 5 and d.isoformat() not in holidays

    anchor = datetime.now(_PT).date()
    while not _is_td(anchor):
        anchor -= timedelta(days=1)
    window: list = []
    d = anchor
    while len(window) < 5:
        if _is_td(d):
            window.append(d.isoformat())
        d -= timedelta(days=1)

    def _eff(raw: str) -> str:
        if not raw:
            return ""
        try:
            d2 = date.fromisoformat(raw)
            while not _is_td(d2):
                d2 -= timedelta(days=1)
            return d2.isoformat()
        except Exception as _e:
            logger.warning("_eff: date map failed for %r: %s", raw, _e)
            return raw

    return min(sum(1 for t in day_trades
                   if isinstance(t, dict)
                   and t.get("symbol") != "SYNC"
                   and _eff(t.get("date", "")) in window), 3)


def compute_pdt_for_date(day_trades: list, target_date: date) -> int:
    """
    Count PDT-qualifying day trades for one specific business date.
    Excludes 'SYNC'. Maps weekend/holiday-stamped entries to prior trading day.
    Holiday-aware via PortfolioTracker._market_holidays().
    """
    holidays = PortfolioTracker._market_holidays()

    def _is_td(d: date) -> bool:
        return d.weekday() < 5 and d.isoformat() not in holidays

    def _eff(raw: str) -> str:
        if not raw:
            return ""
        try:
            d2 = date.fromisoformat(raw)
            while not _is_td(d2):
                d2 -= timedelta(days=1)
            return d2.isoformat()
        except Exception as _e:
            logger.warning("_eff: date map failed for %r: %s", raw, _e)
            return raw

    ds = target_date.isoformat()
    return sum(1 for t in day_trades
               if isinstance(t, dict)
               and t.get("symbol") != "SYNC"
               and _eff(t.get("date", "")) == ds)
