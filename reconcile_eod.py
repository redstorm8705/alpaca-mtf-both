"""
reconcile_eod.py — Post-market EOD P&L reconciliation (TB-P1 + TB-P3 + TB-P4)

Purpose:
  1. Pull actual FILL activities from Alpaca for a trading date.
  2. Match fills to closed trades in the EOD log file.
  3. Correct exit_price, pnl, pnl_remaining, _pnl_source in each trade.
  4. Recompute pnl_today, win_rate_today, alpaca_pnl, score_buckets, score_16pt_buckets.
  5. Write pnl_drift = 0.0 (drift is resolved).
  6. Fire a Slack WARNING if |tracker_pnl - alpaca_pnl| > $0.50 before correction.
  7. Tag every corrected trade with _pnl_source="alpaca_fill".

Usage:
  python3 reconcile_eod.py              # today's EOD file
  python3 reconcile_eod.py 2026-04-20   # specific date

Scheduling (OCI cron, 20:10 UTC = 4:10 PM ET Mon-Fri):
  10 20 * * 1-5 /usr/local/bin/python3 /home/ubuntu/mtf-bot/reconcile_eod.py \
      >> /home/ubuntu/mtf-bot/logs/reconcile.log 2>&1

Data tier: T1 (Alpaca Paper Trading REST API).

AWP audit fix (2026-06-30): RTH Block removed (Rafael mandate) -- this script
may now run at any time, including RTH. HOWEVER: a full domain audit (Harris/
Brandt/Thorp/Taleb lens) found that naively removing the block alone would
open a real data-corruption path. The live bot (strategy/run_cycle.py) writes
this same eod_{date}.json file every ~5-minute cycle during RTH UNLESS the
"_reconcile_ts" sentinel is already set -- write_eod_summary() itself has zero
awareness of the sentinel; it's purely a caller-side gate. If this script ran
mid-RTH and set the sentinel before the trading day's fills were complete
(GTC/partial fills can land hours later), the live bot would stop writing
fresh EOD snapshots for the rest of the session while this script's
necessarily-incomplete reconciliation became "final." Fix: the sentinel is
now only set if run after market close (16:00 ET) -- see _check_post_close()
below. Before that, the script still runs and writes its best-effort
reconciliation, but leaves the sentinel unset, so the live bot's periodic
flush continues to refresh the EOD file every cycle until a complete,
post-market reconciliation actually finalizes it.
"""

import os
import sys
import json
import logging
import requests  # type: ignore[import-untyped]
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Path anchoring — always relative to this file, never CWD
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent
_LOGS_DIR     = _PROJECT_ROOT / "logs"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RECONCILE] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_ET           = ZoneInfo("America/New_York")
_PT           = ZoneInfo("America/Los_Angeles")
_PAPER_BASE   = "https://paper-api.alpaca.markets"
_TIMEOUT      = 10.0          # seconds
_DRIFT_THRESH = 0.50          # Slack alert threshold in dollars
_FILL_WINDOW  = 120           # seconds: match fills within ±2 min of tracker exit_time

# ---------------------------------------------------------------------------
# Post-close check — gates the R-GUARD sentinel, not script execution
# ---------------------------------------------------------------------------
def _is_post_close(now: datetime | None = None) -> bool:
    """
    True if it's at or after 4:00 PM ET (market close). Used to decide whether
    this run's reconciliation is allowed to set the "_reconcile_ts" R-GUARD
    sentinel (see module docstring for why: the sentinel tells the live bot
    to stop overwriting the EOD file, which is only safe once the trading
    day's fills are actually complete).
    """
    _now = now or datetime.now(_ET)
    return (_now.hour * 60 + _now.minute) >= (16 * 60)

# ---------------------------------------------------------------------------
# Alpaca API helpers
# ---------------------------------------------------------------------------
def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
    }

def _fetch_fills(date_str: str) -> list[dict]:
    """
    Pull all FILL activities for a calendar date (ET timezone window).
    Returns list of fill dicts from Alpaca /v2/account/activities/FILL.
    Paginates via page_token to get all fills (after_id is IGNORED by Alpaca).
    """
    # Build ET midnight-to-23:59 window in ISO-8601
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=0, minute=0, second=0, tzinfo=_ET
    )
    day_end = day_start + timedelta(hours=23, minutes=59, seconds=59)

    page_token: str | None = None
    all_fills: list[dict] = []

    while True:
        url = (
            f"{_PAPER_BASE}/v2/account/activities/FILL"
            f"?after={day_start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
            f"&until={day_end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
            f"&page_size=100"
        )
        if page_token:
            url += f"&page_token={page_token}"
        try:
            resp = requests.get(
                url,
                headers=_headers(),
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.error("Alpaca FILL fetch failed: %s", exc)
            return []

        if resp.status_code != 200:
            logger.error("Alpaca FILL fetch HTTP %d: %s", resp.status_code, resp.text[:200])  # noqa: E501
            return []

        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break

        all_fills.extend(batch)
        page_token = batch[-1].get("id")

        if len(batch) < 100:
            break  # last page

    logger.info("Fetched %d FILL activities for %s", len(all_fills), date_str)
    return all_fills

# ---------------------------------------------------------------------------
# Fill matching helpers
# ---------------------------------------------------------------------------
def _match_fills_to_trade(trade: dict, fills: list[dict]) -> list[dict]:
    """
    Find fills that match this trade's symbol + exit side.
    Long trades exit via SELL; short trades exit via BUY.

    No timestamp filtering — the bot logs order submission time, not fill time.
    Pre-market and GTC orders can fill hours after the tracker exit_time.
    Matching is by symbol+side within the date window; duplicate fills are
    de-duplicated by fill id.
    """
    symbol    = trade.get("symbol", "").upper()
    direction = trade.get("direction", "long")
    exit_side = "sell" if direction == "long" else "buy"

    matched: list[dict] = []
    seen_ids: set[str] = set()
    for fill in fills:
        if fill.get("symbol", "").upper() != symbol:
            continue
        fill_side = fill.get("side", "").lower()
        # Alpaca returns "sell" or "sell_short" for short exits (buy to cover)
        # and "buy" or "buy_to_cover" for long exits — normalize
        if exit_side == "sell" and fill_side not in ("sell",):
            continue
        if exit_side == "buy" and fill_side not in ("buy", "buy_to_cover"):
            continue
        fill_id = fill.get("id", "")
        if fill_id in seen_ids:
            continue
        seen_ids.add(fill_id)
        matched.append(fill)

    return matched

def _weighted_avg_exit_price(
    matched_fills: list[dict], qty_remaining: int, partial_qty: int = 0
) -> tuple[float | None, int]:
    """
    Compute FIFO weighted-average exit price for qty_remaining shares.

    When partial_qty > 0, the earliest fills (by timestamp) are the partial
    exit tranche and must be skipped before computing the final exit avg.
    Fills are sorted ascending (oldest first) so FIFO skipping is correct.

    Returns (exit_price, actual_qty) where actual_qty is the fill-confirmed
    share count. actual_qty may be < qty_remaining (ghost-share condition).
    """
    if not matched_fills or qty_remaining <= 0:
        return None, 0

    # Sort by fill timestamp ascending (partial exits happen first)
    def _sort_key(f: dict) -> str:
        return f.get("transaction_time") or f.get("filled_at") or ""

    sorted_fills = sorted(matched_fills, key=_sort_key)

    # Skip the first partial_qty shares (FIFO = partial exit tranche)
    skipped = 0
    remaining_fills: list[dict] = []
    skipped_inner = 0
    for fill in sorted_fills:
        try:
            qty = int(fill.get("qty", 0))
        except (TypeError, ValueError):
            skipped_inner += 1
            continue
        if skipped < partial_qty:
            take = min(qty, partial_qty - skipped)
            skipped += take
            leftover = qty - take
            if leftover > 0:
                # Partial overlap — take remainder of this fill
                remaining_fills.append({**fill, "qty": str(leftover)})
        else:
            remaining_fills.append(fill)

    if skipped_inner > 0:
        logger.warning(
            "_weighted_avg_exit_price: %d fill(s) skipped in FIFO loop — malformed qty field. Exit price may be biased.",  # noqa: E501
            skipped_inner,
        )

    # Weighted average over final-exit fills up to qty_remaining
    total_value  = 0.0
    total_shares = 0
    skipped_outer = 0
    for fill in remaining_fills:
        if total_shares >= qty_remaining:
            break
        try:
            qty   = int(fill.get("qty", 0))
            price = float(fill.get("price", 0.0))
        except (TypeError, ValueError):
            skipped_outer += 1
            continue
        take          = min(qty, qty_remaining - total_shares)
        total_value  += take * price
        total_shares += take

    if skipped_outer > 0:
        logger.warning(
            "_weighted_avg_exit_price: %d fill(s) skipped in weighted-avg loop — malformed qty/price field. Exit price may be biased.",  # noqa: E501
            skipped_outer,
        )

    if total_shares == 0:
        return None, 0
    return round(total_value / total_shares, 4), total_shares

# ---------------------------------------------------------------------------
# P&L recomputation
# ---------------------------------------------------------------------------
def _compute_trade_pnl(
    trade: dict, new_exit_price: float, actual_qty: int | None = None
) -> tuple[float, float]:
    """
    Returns (total_pnl, remaining_pnl) given new exit_price.
    Handles partial exits (partial_exit_price + partial_pnl already in trade).
    actual_qty: Alpaca fill-confirmed share count (Harris/Kyle: use fills, not tracker).
    """
    entry_price   = float(trade.get("entry_price", 0.0))
    total_qty     = int(trade.get("qty", 1))
    if total_qty <= 0:
        logger.warning(
            "[%s] _compute_trade_pnl: total_qty=%d invalid — returning zero P&L",
            trade.get("symbol", "?"), total_qty,
        )
        return 0.0, 0.0
    qty_remaining = int(trade.get("qty_remaining", total_qty))
    if qty_remaining < 0 or qty_remaining > total_qty:
        qty_remaining = 0  # corrupted qty_remaining — zeroes remaining_pnl
    direction     = trade.get("direction", "long")
    partial_pnl   = float(trade.get("partial_pnl", 0.0))

    shares_for_pnl = (
        actual_qty if actual_qty is not None and actual_qty > 0 else qty_remaining
    )
    sign = 1.0 if direction == "long" else -1.0
    remaining_pnl = round(sign * (new_exit_price - entry_price) * shares_for_pnl, 2)
    total_pnl     = round(partial_pnl + remaining_pnl, 2)
    return total_pnl, remaining_pnl

# ---------------------------------------------------------------------------
# Score bucket recomputation
# ---------------------------------------------------------------------------
def _rebuild_score_buckets(trades: list[dict], score_key: str = "score") -> dict:
    """Recompute score bucket stats from corrected trades.

    Uses pnl_remaining when partial_exit is on a prior day; full pnl otherwise.
    """
    buckets: dict = {}
    for t in trades:
        raw_score = t.get(score_key)
        if raw_score is None:
            continue
        try:
            score = int(raw_score)
        except (TypeError, ValueError):
            continue
        lo = (score // 2) * 2
        hi = lo + 1
        key = f"{lo}-{hi}"
        if key not in buckets:
            buckets[key] = {"wins": 0, "losses": 0, "pnl": 0.0}
        # Use pnl_remaining only when partial exit was on a prior day
        def _pt_same(tr: dict) -> bool:
            pt = (tr.get("partial_exit_time") or "")[:10]
            et = (tr.get("exit_time") or "")[:10]
            return bool(pt and et and pt == et)
        if t.get("partial_exited") and not _pt_same(t):
            pnl = float(t.get("pnl_remaining", t.get("pnl", 0.0)))
        else:
            pnl = float(t.get("pnl", 0.0))
        if pnl >= 0:
            buckets[key]["wins"] += 1
        else:
            buckets[key]["losses"] += 1
        buckets[key]["pnl"] = round(buckets[key]["pnl"] + pnl, 2)
    return buckets

# ---------------------------------------------------------------------------
# Slack alert (standalone — no bot import)
# ---------------------------------------------------------------------------
def _slack_warn(msg: str) -> None:
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        logger.warning("SLACK_WEBHOOK_URL not set — cannot send alert: %s", msg)
        return
    try:
        resp = requests.post(
            webhook,
            json={"text": f":warning: *reconcile_eod* {msg}"},
            timeout=5.0,
        )
        if resp.status_code != 200:
            logger.warning("Slack POST returned %d", resp.status_code)
    except Exception as exc:
        logger.warning("Slack alert failed: %s", exc)

# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------
def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON to a tmp file then replace() — never leaves a half-written file."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            json.dump(data, fh, indent=2)
        Path(tmp_path).replace(path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError as _unlink_err:
            # Cleanup failure in post-market cron context — debug level sufficient;
            # primary error propagates via outer raise regardless.
            logger.debug(
                "_atomic_write: tmp cleanup failed (ignored — "
                "original exception will propagate): %s",
                _unlink_err,
            )
        raise

# ---------------------------------------------------------------------------
# SIGKILL recovery helper
# ---------------------------------------------------------------------------
def _recover_closed_trades_from_log(date_str: str) -> list[dict]:
    """
    When the primary EOD writer (write_eod_summary) ran with stale in-memory
    state after a SIGKILL restart, the EOD file may have trades=[]. By the time
    reconcile runs (4:10 PM ET), the bot's orphan reconciler has already
    re-persisted SIGKILL-missed exits to trade_log.json. Read it here and
    return closed trades whose exit_time starts with date_str.
    """
    trade_log_path = _PROJECT_ROOT / "trade_log.json"
    if not trade_log_path.exists():
        logger.warning("trade_log.json not found — SIGKILL recovery unavailable.")
        return []
    try:
        with open(trade_log_path) as _f:
            tl = json.load(_f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("trade_log.json read failed: %s — SIGKILL recovery skipped.", exc)
        return []

    recovered = [
        t for t in tl.get("closed", [])
        if (t.get("exit_time") or "").startswith(date_str)
    ]
    if recovered:
        logger.info(
            "SIGKILL recovery: %d closed trade(s) found in trade_log.json for %s.",
            len(recovered), date_str,
        )
    return recovered


# ---------------------------------------------------------------------------
# Main reconciliation logic
# ---------------------------------------------------------------------------
def reconcile(date_str: str, suppress_slack: bool = False) -> None:
    eod_path = _LOGS_DIR / f"eod_{date_str}.json"
    if not eod_path.exists():
        logger.error("EOD file not found: %s", eod_path)
        sys.exit(1)

    with open(eod_path) as fh:
        eod = json.load(fh)

    trades = eod.get("trades", [])
    closed = [t for t in trades if t.get("status") == "closed"]
    if not closed:
        # SIGKILL recovery: primary writer may have run with stale in-memory state.
        # By 4:10 PM ET the bot's orphan reconciler has re-persisted missed exits.
        _recovered = _recover_closed_trades_from_log(date_str)
        if not _recovered:
            logger.info(
                "No closed trades in %s or trade_log.json — nothing to reconcile.",
                eod_path.name,
            )
            return
        # Assign both so in-place mutations in the reconcile loop are reflected
        # in the final eod["trades"] = trades write at line ~472.
        closed = _recovered
        trades = _recovered

    fills = _fetch_fills(date_str)
    if not fills:
        logger.warning("No FILL activities returned from Alpaca for %s — skipping.", date_str)  # noqa: E501
        return

    original_tracker_pnl = float(eod.get("tracker_pnl") or eod.get("pnl_today", 0.0))
    corrections: list[str] = []
    any_corrected = False

    for trade in closed:
        symbol = trade.get("symbol", "?")
        matched = _match_fills_to_trade(trade, fills)
        if not matched:
            logger.warning("[%s] No matching fills found — leaving as-is.", symbol)
            continue

        total_qty     = int(trade.get("qty", 1))
        if total_qty <= 0:
            logger.warning(
                "[%s] total_qty=%d invalid — skipping reconcile.", symbol, total_qty
            )
            continue
        qty_remaining = int(trade.get("qty_remaining", total_qty))
        if qty_remaining < 0 or qty_remaining > total_qty:
            logger.warning(
                "[%s] qty_remaining=%d out of bounds [0, %d] — falling back to 0 "
                "(FIFO price lookup returns None; trade left as-is)",
                symbol, qty_remaining, total_qty,
            )
            qty_remaining = 0
        partial_qty   = total_qty - qty_remaining  # shares already exited via partial
        new_exit, actual_qty = _weighted_avg_exit_price(
            matched, qty_remaining, partial_qty
        )
        actual_qty = min(actual_qty, total_qty)  # Harris-Edge-C: cap at position size
        trade["_qty_at_close"] = actual_qty  # Harris-2: set before None check for audit
        if new_exit is None:
            logger.warning(  # noqa: E501
                "[%s] Could not compute avg exit price — leaving as-is.", symbol
            )
            continue
        if actual_qty < qty_remaining:
            _ghost_msg = (
                f"[{symbol}] Ghost shares: {actual_qty} fill-confirmed vs "
                f"{qty_remaining} tracker qty_remaining — "
                f"{qty_remaining - actual_qty} unconfirmed share(s). "
                "Check position state."
            )
            logger.warning(_ghost_msg)
            if not suppress_slack:
                _slack_warn(_ghost_msg)
        elif actual_qty != qty_remaining:
            logger.warning(
                "[%s] Fill qty=%d > tracker qty_remaining=%d — overshoot, possible stale tracker. Using fill qty.",  # noqa: E501
                symbol, actual_qty, qty_remaining,
            )

        old_exit = float(trade.get("exit_price", 0.0))
        if abs(new_exit - old_exit) < 0.005:
            logger.info("[%s] Exit price unchanged ($%.4f) — no correction needed.", symbol, old_exit)  # noqa: E501
            trade["_pnl_source"]        = "alpaca_fill"
            trade["_fill_unverified"]   = False
            trade["_fill_reconciled_at"] = datetime.now(_ET).isoformat()
            continue

        # Compute corrected P&L
        new_pnl, new_pnl_remaining = _compute_trade_pnl(trade, new_exit, actual_qty=actual_qty)  # noqa: E501
        old_pnl = float(trade.get("pnl", 0.0))

        logger.info(
            "[%s] Correcting exit $%.4f → $%.4f | P&L $%.2f → $%.2f",
            symbol, old_exit, new_exit, old_pnl, new_pnl,
        )
        corrections.append(
            f"{symbol}: exit ${old_exit:.2f}→${new_exit:.2f} pnl ${old_pnl:.2f}→${new_pnl:.2f}"  # noqa: E501
        )

        trade["exit_price"]        = new_exit
        trade["pnl"]               = new_pnl
        trade["pnl_remaining"]     = new_pnl_remaining
        trade["_pnl_source"]        = "alpaca_fill"
        trade["_fill_unverified"]   = False
        trade["_fill_reconciled_at"] = datetime.now(_ET).isoformat()
        trade["_pnl_note"]          = (
            f"exit_price corrected from tracker ${old_exit:.2f} to Alpaca fill ${new_exit:.4f} "  # noqa: E501
            f"(reconcile_eod {datetime.now(_ET).strftime('%Y-%m-%d')})"
        )
        any_corrected = True

    if not any_corrected:
        logger.info("No exit price corrections needed for %s — recomputing aggregates anyway.", date_str)  # noqa: E501

    # Always recompute EOD-level aggregates (idempotent re-run safety).
    # pnl_today: use full pnl if partial exit happened on the same date as final exit
    # (both events are on today's P&L); use only pnl_remaining when partial exit was
    # on a prior day (already counted then, so only remaining shares count today).
    def _partial_on_same_day(t: dict) -> bool:
        pt = (t.get("partial_exit_time") or "")[:10]
        et = (t.get("exit_time") or "")[:10]
        return bool(pt and et and pt == et)

    def _day_pnl(t: dict) -> float:
        if t.get("partial_exited") and not _partial_on_same_day(t):
            return float(t.get("pnl_remaining", t.get("pnl", 0.0)))
        return float(t.get("pnl", 0.0))

    new_pnl_today  = round(sum(_day_pnl(t) for t in closed), 2)
    # Exclude breakeven trades (pnl=0.0) from both numerator and denominator.
    # DS+GAI consensus S20: scratches are not risk outcomes and inflate win rate.
    _resolved      = [t for t in closed if _day_pnl(t) != 0.0]
    wins           = sum(1 for t in _resolved if _day_pnl(t) > 0)
    new_win_rate   = round(100.0 * wins / len(_resolved), 1) if _resolved else 0.0
    new_score_bkts = _rebuild_score_buckets(closed, score_key="score")
    new_16pt_bkts  = _rebuild_score_buckets(closed, score_key="score_16pt")

    # Drift check: compare original tracker P&L vs new Alpaca-sourced total
    drift = abs(original_tracker_pnl - new_pnl_today)
    if drift > _DRIFT_THRESH:
        msg = (
            f"P&L drift >{_DRIFT_THRESH:.2f} on {date_str}: "
            f"tracker ${original_tracker_pnl:.2f} → corrected ${new_pnl_today:.2f} "
            f"(delta ${drift:.2f}). Changes: {'; '.join(corrections)}"
        )
        logger.warning(msg)
        if not suppress_slack:
            _slack_warn(msg)

    # Patch EOD object
    eod["pnl_today"]            = new_pnl_today
    eod["win_rate_today"]       = new_win_rate
    eod["alpaca_pnl"]           = new_pnl_today
    eod["tracker_pnl"]          = original_tracker_pnl
    eod["pnl_drift"]            = 0.0
    eod["score_buckets"]        = new_score_bkts
    if new_16pt_bkts:
        eod["score_16pt_buckets"] = new_16pt_bkts
    eod["trades"]               = trades  # closed trades updated in-place
    # P0 2026-07-05: clear the write_eod_summary unreconciled flag ONLY if every
    # closed trade is now confirmed against a real Alpaca fill. If any trade
    # couldn't be matched (the RC-4 root: closing fills absent/mismatched),
    # leave the flag set so the day still renders "review" downstream rather
    # than falsely asserting a completed reconciliation.
    _all_fill_sourced = bool(closed) and all(
        t.get("_pnl_source") == "alpaca_fill" for t in closed
    )
    if _all_fill_sourced:
        eod["pnl_unreconciled"]        = False
        eod["pnl_unreconciled_reason"] = None
    elif eod.get("pnl_unreconciled"):
        eod["pnl_unreconciled_reason"] = "reconcile_partial_unmatched_fills"
        logger.warning(
            "%s: %d/%d closed trade(s) NOT fill-confirmed — leaving day flagged "
            "unreconciled (reason=reconcile_partial_unmatched_fills).",
            date_str,
            sum(1 for t in closed if t.get("_pnl_source") != "alpaca_fill"),
            len(closed),
        )
    # R-GUARD sentinel — bot will not overwrite after this timestamp is set.
    # AWP audit fix (2026-06-30): only set once the trading day's fills are
    # actually complete (post-close). Setting it mid-RTH would freeze the
    # live bot's periodic EOD writes against a necessarily-incomplete
    # reconciliation (GTC/partial fills can land hours later). If run before
    # close, this reconciliation still writes its best-effort numbers, but
    # leaves the sentinel unset so the live bot keeps refreshing the file.
    if _is_post_close():
        eod["_reconcile_ts"] = datetime.now(_ET).isoformat()
    else:
        logger.warning(
            "Running before market close (%s ET) — _reconcile_ts NOT set. "
            "This run's reconciliation may be based on incomplete fills "
            "(GTC/partial orders can fill later today). The live bot will "
            "continue overwriting this file each cycle until a post-close "
            "run finalizes it.",
            datetime.now(_ET).strftime("%H:%M"),
        )

    _atomic_write(eod_path, eod)
    logger.info(
        "Reconcile complete for %s: pnl_today=$%.2f win_rate=%.1f%% (%d corrections)",
        date_str, new_pnl_today, new_win_rate, len(corrections),
    )

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Load .env if present (local/Mac runs)
    _env_path = _PROJECT_ROOT / ".env"
    if _env_path.exists():
        with open(_env_path) as _ef:
            for _line in _ef:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    os.environ.setdefault(_k.strip(), _v.strip())

    _flags = [a for a in sys.argv[1:] if a.startswith("--")]
    _positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    _suppress_slack = "--no-slack" in _flags
    _date = _positional[0].strip() if _positional else datetime.now(_ET).strftime("%Y-%m-%d")  # noqa: E501

    logger.info("Starting EOD reconciliation for %s (suppress_slack=%s)", _date, _suppress_slack)  # noqa: E501
    reconcile(_date, suppress_slack=_suppress_slack)
