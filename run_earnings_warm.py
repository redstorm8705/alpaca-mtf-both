# ruff: noqa: E501  — dense rationale lines run long (project convention)
"""run_earnings_warm.py — keep the per-symbol earnings-date cache fresh outside the trading process (EARN-SOURCE-1029).

Cron (OCI crontab, through scripts/cron_tz_wrapper.py): weekdays 08:30, 09:15 and 12:30 ET, and Sunday 17:45 ET
(before the 17:55 ET earnings preflight). Calls data.fmp_client.warm_symbol_earnings(), which asks Finnhub per symbol
(FMP fallback, UNKNOWN when neither covers it), writes data/cache/earnings_symbol_cache.json under the earnings lock
with an atomic replace, and logs one EARNWARM line per symbol plus a summary. The trading process then serves those
entries from cache. Design: logs/design_records/earnings_source_finnhub_2026-10-29.md.
"""
import json
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import config  # noqa: E402
from data import fmp_client  # noqa: E402

logger = logging.getLogger("run_earnings_warm")
ET = ZoneInfo("America/New_York")


def _ledger_symbols() -> list:
    """Symbols the ownership ledger holds with a finite, non-zero net quantity."""
    try:
        led = json.loads((ROOT / "data" / "state" / "ownership_ledger.json").read_text())
    except Exception as e:  # noqa: BLE001
        logger.warning("earnings warm: ownership ledger unreadable (%s)", e)
        return []
    positions = led.get("positions") if isinstance(led, dict) else None
    if not isinstance(positions, dict):
        logger.warning("earnings warm: ownership ledger has no positions map")
        return []
    out = []
    for sym, pos in positions.items():
        try:
            qty = float((pos or {}).get("alpaca_net_qty") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if math.isfinite(qty) and qty != 0:
            out.append(sym)
    return out


def symbols() -> list:
    """Held names first (ledger), then the watchlist, the Day-tier universes, and QHM picks + holds, the F6 universe
    and the Swing pool (execution.dip_zones.universe)."""
    names: list = _ledger_symbols()
    names += list(getattr(config, "WATCHLIST", []) or [])
    names += list(getattr(config, "DAY_TIER_UNIVERSE", []) or [])
    names += list(getattr(config, "DAYTRADE_UNIVERSE", []) or [])
    try:
        from execution import dip_zones
        names += dip_zones.universe()
    except Exception as e:  # noqa: BLE001
        logger.warning("earnings warm: QHM/F6/Swing universe unavailable (%s)", e)
        names += list(getattr(config, "FOREVER6_UNIVERSE", []) or [])
    return list(dict.fromkeys(str(s).upper().strip() for s in names if s and str(s).strip()))


def next_close_utc(now_et: "datetime | None" = None) -> datetime:
    """The next regular-session close (config.TOD_MARKET_CLOSE, minutes after midnight ET) as a UTC datetime: today's
    if it is still ahead, else tomorrow's. Entries are refreshed when they would expire before it, so a date never
    goes stale during a session between warm runs. Weekends/holidays only make this earlier than the real next close,
    which refreshes more, never less; the next morning run covers the session itself."""
    now = now_et or datetime.now(ET)
    close = datetime.combine(now.date(), dtime.min, tzinfo=ET) + timedelta(minutes=config.TOD_MARKET_CLOSE)
    if close <= now:
        close += timedelta(days=1)
    return close.astimezone(timezone.utc)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    syms = symbols()
    until = next_close_utc()
    logger.info("EARNWARM start symbols=%d refresh_until=%s", len(syms), until.isoformat())
    fmp_client.warm_symbol_earnings(syms, refresh_until=until)
    return 0


if __name__ == "__main__":
    sys.exit(main())
