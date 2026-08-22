"""
execution/fifo_pnl.py — Alpaca-fills FIFO P&L reconstruction + lot-state persistence.

Extracted verbatim from portfolio_tracker.py in M1 (2026-07-06), ZERO logic
change. Depends only on execution.state_io (a leaf module) — the dependency
graph is a DAG: portfolio_tracker → fifo_pnl → state_io.

Phase 2: Alpaca fills as EOD P&L authority (board vote 2026-04-19, 26-0).
"""

import json
import os
import re
import logging
import urllib.request
import urllib.parse
import time as _time_mod
from datetime import datetime

from execution.state_io import _ET, _PT, _SSL_CTX, _atomic_write, _LOTS_STATE_FILE

logger = logging.getLogger(__name__)

# _LOTS_STATE_FILE is imported from state_io (single source of truth for the
# lot-state path — M1 hardening). _ALPACA_PAPER_BASE stays here (fetch-specific).
_ALPACA_PAPER_BASE = "https://paper-api.alpaca.markets"


# ── Phase 2: Alpaca fills FIFO helpers ───────────────────────────────────────

def _parse_alpaca_ts(ts_str: str) -> datetime:
    """Tolerant ISO-8601 parser for Alpaca timestamps (B1 fix 2026-07-03,
    board+Gro+GAI). Python 3.10 fromisoformat accepts only exactly 3 or 6
    fractional-second digits; Alpaca emits variable precision (live repro:
    '2026-07-02T15:32:55.77875Z' — 5 digits — raised "Invalid isoformat
    string" on every EOD flush). Normalize: Z→+00:00, fractional seconds
    padded/truncated to 6 digits. Raises ValueError on genuinely malformed
    input — caller decides the fallback."""
    s = str(ts_str).strip().replace("Z", "+00:00")
    m = re.match(r"^(.*?[T ]\d{2}:\d{2}:\d{2})\.(\d+)(.*)$", s)
    if m:
        head, frac, tail = m.groups()
        s = f"{head}.{frac[:6].ljust(6, '0')}{tail}"
    return datetime.fromisoformat(s)


def _fill_et_date(ts_str: str) -> str:
    """Convert Alpaca transaction_time UTC ISO timestamp to ET date string."""
    try:
        dt = _parse_alpaca_ts(ts_str)
        return dt.astimezone(_ET).strftime("%Y-%m-%d")
    except Exception as _e:
        # Last-resort fallback: raw UTC date slice. WARNING (not silent) because
        # for fills between 8 PM and midnight ET the UTC date is TOMORROW's ET
        # date — such a fill can land on the wrong FIFO day. The tolerant parser
        # above makes this path unreachable for every Alpaca format seen to date.
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


def _fifo_reconstruct(
    fills: list, prior_lots: dict, processed_fill_ids: "set | None" = None,
    protected_close_order_ids: "set | None" = None,
) -> tuple:
    """
    FIFO P&L reconstruction from Alpaca fills.

    prior_lots: {symbol: [{"qty": N, "price": P, "side": "long"|"short"}]}
                Open lots carried forward from the prior trading day.

    processed_fill_ids: fill IDs already folded into prior_lots earlier
        TODAY (see _load_prior_day_lots). Fills whose ID is in this set are
        skipped — without this, calling write_eod_summary() more than once
        per day (it's called from 6 sites: SIGTERM, heartbeat recovery,
        user shutdown, AH cycle, market-close, periodic flush) re-applies
        every still-open same-day fill on every call, appending a duplicate
        lot each time. Confirmed in production 2026-06-27 (AMD/PANW/SMCI/
        NVDA each had 30-80 duplicate lots; a stale NVDA lot caused a real
        EOD P&L drift). Board + Gro + GAI consensus fix.

    protected_close_order_ids: fill order_ids that are protected-tier (QHM/forever6)
        protective-stop CLOSES. Skipped from intraday FIFO (like QHM symbols) so a
        legit tier stop-out isn't recorded as a phantom synthetic short when its
        symbol has already left the QHM list at close-time. None = empty = no-op.

    Handles mixed buy/buy_to_cover labels via net-position-aware matching:
    a "buy" that arrives when net position is short is treated as a cover.

    Returns:
        today_realized_pnl (float)
        remaining_lots     (dict)  — end-of-day open lot structure
        per_trade          (list)  — [{symbol, side, qty, entry, exit, pnl, filled_at}]
        seen_fill_ids       (set)   — processed_fill_ids plus every fill ID
                                       processed this call (persist via
                                       _save_open_lots_state)
    """
    import copy
    # QHM ownership (Option B step 2, 2026-07-01): QHM is tracked separately (its
    # own HoldPosition state machine) — exclude it from intraday EOD FIFO P&L.
    # Purge any QHM lots carried in prior_lots (self-healing cleanup of stale QHM
    # entries — board FIFO requirement) AND skip QHM fills in the loop below, so
    # QHM never contributes to today_pnl / remaining_lots. Lazy import avoids
    # circular-import risk in this foundational module.
    from execution.quarterly_hold_manager import get_quarterly_hold_symbols
    _qhm_syms = get_quarterly_hold_symbols()
    # Tag-based protected-tier close skip (P1 2026-08-21): the symbol-based QHM skip
    # below misses a protected (QHM/forever6) protective-stop CLOSE whose symbol already
    # dropped from get_quarterly_hold_symbols() at close-time — the race that recorded
    # LLY's legit +$40.88 stop-out as a phantom "synthetic short". The caller supplies
    # the set of fill order_ids that are protected closes (found without a blocking
    # Alpaca call); those fills skip intraday FIFO like QHM symbols. Default None =
    # empty set = byte-identical to prior behaviour (safe no-op).
    _protected_oids = protected_close_order_ids or set()
    lots      = copy.deepcopy(prior_lots)
    for _qs in list(_qhm_syms):
        lots.pop(_qs, None)
    today_pnl = 0.0
    per_trade = []
    seen_fill_ids = set(processed_fill_ids or set())

    for fill in fills:
        fill_id   = fill.get("id", "")
        sym       = fill.get("symbol", "")
        if sym in _qhm_syms:
            continue   # QHM tracked separately — excluded from intraday EOD FIFO P&L
        # Order_id-tagged protected close (catches the close-time race where the symbol
        # already left the QHM list) — skipped from intraday FIFO like a QHM symbol.
        _foid = fill.get("order_id", "")
        if _foid and _foid in _protected_oids:
            # Mark seen so a later same-day run whose protected set no longer has this
            # order_id (stop cleared on a state transition) can't reprocess it as a
            # synthetic short. cold-2nd 2026-08-21.
            if fill_id:
                seen_fill_ids.add(fill_id)
            continue
        side      = fill.get("side", "")
        qty       = int(float(fill.get("qty", 0)))
        price     = float(fill.get("price", 0))
        filled_at = fill.get("transaction_time", "")

        if not sym or qty <= 0:
            continue
        if fill_id and fill_id in seen_fill_ids:
            continue  # already folded into lots by an earlier call today
        if fill_id:
            seen_fill_ids.add(fill_id)

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
                    # S49 board unanimous (McKinney/Derman/Peterffy): DO NOT append
                    # synthetic lot. Phantom lots persist into open_lots_prior_day.json
                    # and accumulate across restarts (AMZN -34→-35→-36 this week).
                    # CRITICAL log forces operator investigation. P2: add Slack dedup.
                    logger.critical(
                        "[FIFO] %s: buy_to_cover with no open short lots "
                        "(net_qty=%d, qty=%d, price=%.2f). "
                        "Prior lots missing — qty NOT recorded. "
                        "Operator review required: check open_lots_prior_day.json.",
                        sym, net_qty, qty, price,
                    )
                    # Intentionally no lot append — prevents phantom accumulation.
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
            elif side == "sell_short":
                # Legitimate short OPEN (or add to an existing short): having no
                # open long lot to match is EXPECTED for a deliberate short, not
                # state corruption. Opening a position realizes no P&L, so record
                # the short lot. Gating this apart from a plain `sell` (below) is
                # the whole point — an intentional sell_short must NOT fire the
                # state-corruption CRITICAL / Slack alert. (Fix 2026-07-24:
                # SMCI/RBLX/MSTR short entries each fired a false CRITICAL.)
                #
                # SAFETY NOTE: this silence trusts Alpaca's `sell_short` label to
                # discriminate a short-open from a long-close. Alpaca labels a
                # long-reducing fill `sell` (-> CRITICAL below), never `sell_short`,
                # so a genuine long-lot-loss still alarms. Should a mislabeled fill
                # ever fabricate a phantom short here, the divergence is caught at
                # the write_eod_summary reconciliation layer (reconstructed-vs-
                # Alpaca net position + ledger-P&L-to-equity check), not by this
                # pure leaf. The INFO breadcrumb below keeps every short-open
                # auditable (mirrors the buy_to_cover path, which logs while
                # suppressing its phantom lot).
                logger.info(
                    "[FIFO] %s: recording deliberate short open "
                    "(net_qty=%d, qty=%d, price=%.2f).",
                    sym, net_qty, qty, price,
                )
                current.append({"qty": qty, "price": price, "side": "short"})
            else:
                # Plain `sell` with no open long lot = genuine state corruption: a
                # long-close whose long lots are missing (bot restart / corruption).
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
    return round(today_pnl, 2), remaining_lots, per_trade, seen_fill_ids


def _load_prior_day_lots() -> tuple:
    """Load open lot structure from end of the previous trading day.

    Returns (open_lots, processed_fill_ids). processed_fill_ids is only
    non-empty when the file's stored date matches today — it tracks which
    of TODAY's fills have already been folded into open_lots, so repeat
    same-day calls to write_eod_summary() (SIGTERM, heartbeat recovery,
    AH cycle, market-close, periodic flush) don't re-append a duplicate
    lot for the same fill every time they run. Confirmed bug 2026-06-27:
    with no fill-level dedup, any position that opened but stayed open
    same-day accumulated one duplicate lot per call — AMD/PANW/SMCI/NVDA
    each had 30-80 duplicate lots in production. A stale 6-week-old NVDA
    lot got matched ahead of a real same-day close, producing a phantom
    P&L drift. Board + Gro + GAI all confirmed this root cause 2026-06-27.
    """
    try:
        if _LOTS_STATE_FILE.exists():
            with open(_LOTS_STATE_FILE) as f:
                data = json.load(f)
            stored_date = data.get("date", "")
            today_dt = datetime.now(_PT).date()
            is_same_day = False
            if stored_date:
                try:
                    stored_dt = datetime.strptime(stored_date, "%Y-%m-%d").date()
                    age_days = (today_dt - stored_dt).days
                    is_same_day = age_days == 0
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
            _processed_ids = (
                set(data.get("processed_fill_ids", [])) if is_same_day else set()
            )
            return data.get("open_lots", {}), _processed_ids
    except Exception as exc:
        logger.warning(f"Could not load prior day lots state: {exc}")
    return {}, set()


def _save_open_lots_state(
    lots: dict, date_str: str, processed_fill_ids: "set | None" = None,
    today_pnl: float = 0.0, per_trade: "list | None" = None,
):
    """Persist end-of-day open lot structure for the next day's FIFO seeding.

    processed_fill_ids: fill IDs already folded into `lots` for `date_str` —
    persisted so a later same-day call can skip them (see _load_prior_day_lots).

    today_pnl / per_trade: the day's ACCUMULATED Alpaca-FIFO realized P&L and
    per-trade attribution for `date_str`. Persisted so a repeat same-day
    write_eod_summary() call — which skips all already-processed fills and so
    reconstructs an EMPTY per_trade / $0 pnl for its own pass — can reload and
    reuse the earlier run's attribution instead of falsely reporting
    attributed=0 (the alpaca_fifo_unattributed A-4 artifact, 2026-07-06
    S-FIFO). Only reused when the reader's date matches (see
    _load_today_attribution).
    """
    try:
        _LOTS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(_LOTS_STATE_FILE, {
            "date": date_str,
            "open_lots": lots,
            "processed_fill_ids": sorted(processed_fill_ids or set()),
            "alpaca_today_pnl": round(float(today_pnl or 0.0), 2),
            "alpaca_per_trade": per_trade or [],
        })
        logger.info(f"Open lots state saved for {date_str}: {len(lots)} symbol(s)")
    except Exception as exc:
        logger.warning(f"Could not save open lots state: {exc}")


def _load_today_attribution() -> tuple:
    """Return (today_pnl, per_trade) previously persisted by an earlier
    write_eod_summary() run TODAY — else the empty baseline (0.0, []).

    Mirrors _load_prior_day_lots' same-day gate: attribution is reused only
    when the state file's stored date == today (PT). On a fresh day the prior
    day's attribution must NOT carry, so the baseline is returned.

    Bridge for the repeat-run FIFO attribution artifact (2026-07-06 S-FIFO):
    a 2nd+ same-day run skips all already-processed fills, so its own
    _fifo_reconstruct pass yields an empty per_trade and $0 pnl. The caller
    accumulates this run's newly-attributed fills ON TOP of this baseline, so
    the day's total attribution survives across every run.
    """
    try:
        if _LOTS_STATE_FILE.exists():
            with open(_LOTS_STATE_FILE) as f:
                data = json.load(f)
            stored_date = data.get("date", "")
            today_str = datetime.now(_PT).strftime("%Y-%m-%d")
            if stored_date == today_str:
                _pt = data.get("alpaca_per_trade", []) or []
                if not isinstance(_pt, list):
                    logger.warning(
                        "[lots] alpaca_per_trade not a list (%s) — ignoring "
                        "persisted attribution baseline", type(_pt).__name__,
                    )
                    return 0.0, []
                # Symmetric numeric guard for alpaca_today_pnl (GAI pre-ship
                # 2026-07-06): a non-numeric stored value must degrade to the
                # baseline, not raise — same contract as the per_trade check.
                _raw_pnl = data.get("alpaca_today_pnl", 0.0)
                try:
                    _pnl = float(_raw_pnl if _raw_pnl is not None else 0.0)
                except (ValueError, TypeError):
                    logger.warning(
                        "[lots] alpaca_today_pnl not numeric (%r) — ignoring "
                        "persisted attribution baseline", _raw_pnl,
                    )
                    return 0.0, []
                return round(_pnl, 2), _pt
    except Exception as exc:
        logger.warning(f"Could not load today attribution baseline: {exc}")
    return 0.0, []
