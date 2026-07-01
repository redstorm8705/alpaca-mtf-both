"""
run_premarket_gate.py
Pre-market reversal-pause gate. Runs once at 9:25 AM ET via cron, scheduled
DST-safe through scripts/cron_tz_wrapper.py (fires 13:25 UTC in EDT / 14:25 UTC
in EST; the wrapper executes only in the matching 09:25 ET window).

Reads logs/premarket_movers.json (ticker list written by data.premarket at
pre-market scan), and for each ticker fetches LIVE at 9:25 AM:
  - prior_close   : T1 daily bar (yesterday's close; today's daily bar not yet formed)
  - pm_high       : T1 1-min bars, max(high) over today's pre-market window (>=04:00 ET)
  - current_price : T1 latest trade (data/alpaca_data.get_latest_trade)

Gate (per symbol) — a symbol qualifies if it gapped up, has retraced >=50% of the
gap back toward prior_close, and is CONSOLIDATING above prior_close (not gap-filled,
not still at the highs):
  gap_retrace_pct = (pm_high - current_price) / (pm_high - prior_close)
  PASS if:
    gap_retrace_pct >= PREMARKET_RETRACE_THRESHOLD (0.50)
    AND current_price > prior_close                                  (still in gap)
    AND current_price <= prior_close + 0.50*(pm_high - prior_close)   (consol. zone)

Score gate is NOT applied here — no RTH intraday bars exist pre-open, so a real
MTF score cannot be computed. The score>=8 gate is applied at 10:05 AM in
run_cycle.py re-validation (where full scoring runs).

Qualifying symbols (max PREMARKET_QUEUE_MAX = 3) are written to
data/state/premarket_queue.json (atomic tmp->replace). run_cycle.py reads this
at its first RTH scan, re-validates, injects with size_override_pct=0.50 and
atr_mult_override=0.625, then clears the queue (idempotent).

Data tier: T1 (Alpaca Data) throughout. No yfinance, no raw requests to market
data endpoints outside the approved data/ modules.
Output: logs/ (log) + data/state/ (queue) — per Guardrail 3 exemptions.
"""

import os
import sys
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

# Anchor all paths to this file's directory (RC-2: no CWD-relative paths)
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from dotenv import load_dotenv                    # noqa: E402
_DOTENV_LOADED = load_dotenv(os.path.join(_HERE, ".env"))  # .env keys under cron

import config                                     # noqa: E402
from data.fetcher import fetch_bars              # T1 bars       # noqa: E402
from data.alpaca_data import get_latest_trade    # T1 last trade # noqa: E402

ET = ZoneInfo("America/New_York")

_LOGS = os.path.join(_HERE, "logs")
_STATE = os.path.join(_HERE, "data", "state")
os.makedirs(_LOGS, exist_ok=True)
os.makedirs(_STATE, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(_LOGS, "premarket_gate.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("premarket_gate")

_MOVERS_PATH = os.path.join(_LOGS, "premarket_movers.json")
_QUEUE_PATH = os.path.join(_STATE, "premarket_queue.json")

# Cap simultaneous queued movers so they never crowd out QHM / standard entries.
PREMARKET_QUEUE_MAX = 3


def _prior_close(symbol: str) -> float | None:
    """Yesterday's daily close (T1). At 9:25 AM the current-day daily bar is not
    yet formed, so iloc[-1] is the prior session's close."""
    df = fetch_bars(symbol, config.TF_DAILY, num_bars=3)
    if df.empty or len(df) < 1:
        return None
    pc = float(df["close"].iloc[-1])
    return pc if pc > 0 else None


def _pm_high(symbol: str) -> float | None:
    """Max high over TODAY's pre-market window (>=04:00 ET, before now) from T1
    1-min bars. Returns None if no pre-market bars are available (caller falls
    back to current price as the high reference)."""
    df = fetch_bars(symbol, config.TF_1M, num_bars=400)
    if df.empty:
        return None
    today = datetime.now(ET).date()
    # Index is tz-aware UTC from Alpaca; convert to ET and keep today's
    # pre-market bars only (04:00 ET .. now). At 9:25 ET no RTH bars exist yet.
    try:
        idx_et = df.index.tz_convert(ET)
    except (TypeError, AttributeError):
        # tz-naive index (unexpected for Alpaca T1) — assume UTC then convert.
        # Log it: a naive index in a non-UTC tz would mis-select the window (GAI RC-3).
        logger.warning(f"[{symbol}] 1-min bar index tz-naive — assuming UTC")
        idx_et = df.index.tz_localize("UTC").tz_convert(ET)
    mask = [
        (ts.date() == today and ts.hour * 60 + ts.minute >= 4 * 60)
        for ts in idx_et
    ]
    session = df[mask]
    if session.empty:
        return None
    hi = float(session["high"].max())
    return hi if hi > 0 else None


def _evaluate(symbol: str) -> dict | None:
    """Return a queue entry dict if the symbol passes the retrace/consolidation
    gate, else None. Any data failure -> None (fail-closed: no queue add)."""
    prior_close = _prior_close(symbol)
    if prior_close is None:
        logger.info(f"[{symbol}] no prior_close — skip")
        return None

    current = get_latest_trade(symbol)
    if current is None:
        logger.info(f"[{symbol}] no current price — skip")
        return None

    pm_high = _pm_high(symbol)
    if pm_high is None:
        # No pre-market bars: use current price as the high reference. With
        # pm_high == current, retrace == 0 -> fails the >=0.50 gate, so the
        # symbol is correctly skipped rather than guessed.
        pm_high = current

    gap = pm_high - prior_close
    if gap <= 0:
        logger.info(
            f"[{symbol}] not a gap-up (pm_high={pm_high:.2f} <= "
            f"prior_close={prior_close:.2f}) — skip"
        )
        return None

    retrace = (pm_high - current) / gap

    # NOTE: ONLY at PREMARKET_RETRACE_THRESHOLD == 0.50 (the current value) is
    # "retrace >= 0.50" exactly equivalent to the old "current <= prior_close +
    # 0.50*gap" consolidation-ceiling check — both reduce to
    # current <= 0.5*pm_high + 0.5*prior_close. They DIVERGE at any other
    # threshold (Gro/GAI/board proof 2026-07-01), so the retrace test is the
    # canonical gate and the redundant ceiling was removed. If the threshold ever
    # changes, re-derive the zone. Combined with current > prior_close this
    # confines entry to (prior_close, midpoint].
    passed = (
        retrace >= config.PREMARKET_RETRACE_THRESHOLD
        and current > prior_close
    )
    logger.info(
        f"[{symbol}] prior_close={prior_close:.2f} pm_high={pm_high:.2f} "
        f"current={current:.2f} retrace={retrace:.0%} -> "
        f"{'PASS' if passed else 'skip'}"
    )
    if not passed:
        return None
    return {
        "symbol": symbol,
        "prior_close": round(prior_close, 2),
        "pm_high": round(pm_high, 2),
        "gate_price": round(current, 2),
        "retrace_pct": round(retrace, 3),
    }


def _load_movers() -> list:
    try:
        with open(_MOVERS_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.warning(f"{_MOVERS_PATH} not found — no pre-market movers to gate")
        return []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"failed reading {_MOVERS_PATH} ({type(e).__name__}): {e}")
        return []
    movers = data.get("movers", [])
    return [s for s in movers if isinstance(s, str)]


def _write_queue(entries: list) -> None:
    payload = {
        "queue": entries,
        "ts": datetime.now(ET).isoformat(),
        "size_override_pct": config.PREMARKET_KELLY_MULT,
        "atr_mult_override": config.PREMARKET_ATR_MULT,
        "min_score": config.PREMARKET_MIN_SCORE,
    }
    tmp = _QUEUE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _QUEUE_PATH)
    logger.info(f"wrote {len(entries)} symbol(s) to {_QUEUE_PATH}: "
                f"{[e['symbol'] for e in entries]}")


def main() -> None:
    now = datetime.now(ET)
    logger.info(f"=== pre-market gate run {now.isoformat()} ===")
    if not _DOTENV_LOADED:
        logger.warning(
            "load_dotenv: .env not found at %s — Alpaca keys may be missing", _HERE)
    movers = _load_movers()
    if not movers:
        _write_queue([])
        return
    logger.info(f"evaluating {len(movers)} mover(s): {movers}")

    qualified = []
    for sym in movers:
        entry = _evaluate(sym)
        if entry is not None:
            qualified.append(entry)

    # Rank by CLEANEST pullback first: a lower retrace (e.g. 0.50 — held the upper
    # half of the gap) is a healthier reversal-pause than a near-gap-fill (e.g.
    # 0.95 — momentum exhausted, about to break prior_close). Board(quant) flagged
    # descending as inverted vs intent. Ascending -> strongest setups fill the
    # capped slots.
    qualified.sort(key=lambda e: e["retrace_pct"])
    capped = qualified[:PREMARKET_QUEUE_MAX]
    if len(qualified) > PREMARKET_QUEUE_MAX:
        logger.info(
            f"queue capped at {PREMARKET_QUEUE_MAX} (cleanest retrace first); dropped "
            f"{[e['symbol'] for e in qualified[PREMARKET_QUEUE_MAX:]]}"
        )
    _write_queue(capped)


if __name__ == "__main__":
    main()
