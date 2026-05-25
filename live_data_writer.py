# ruff: noqa: E501
"""
live_data_writer.py
Writes dashboard.html every 30 seconds with live Alpaca data.
Runs as a background companion to main.py — launched by launch_bots.sh.

Decouples dashboard freshness from the 5-minute scan cycle so Today P&L,
equity, and unrealized position P&L always reflect Alpaca's current state.
"""

import time
import logging
import sys
import os
import fcntl
import ssl
import socket
import urllib.error
from pathlib import Path
from logging.handlers import WatchedFileHandler as _WFH  # logrotate create-mode compat

# LDW-1: absolute path anchor — prevents CWD-relative failures when launched
# from a directory other than the project root (same class as TB-2/M-12/SG-1)
_HERE     = Path(__file__).resolve().parent
_LOGS_DIR = _HERE / "logs"

# ── Logging — separate file so it doesn't pollute bot.log ────────────────────
_LOGS_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | live_writer | %(message)s",
    handlers=[
        _WFH(str(_LOGS_DIR / "live_writer.log")),
    ],
)
logger = logging.getLogger("live_writer")

INTERVAL        = 30                                             # seconds between dashboard writes
LOCK_PATH       = str(_LOGS_DIR / "alpaca_mtf_live_writer.lock")  # P2-3: singleton lock path
# P5-L3: consecutive-failure backoff
_MAX_BACKOFF    = 300    # cap retries at 5 minutes
_ALERT_AFTER    = 5      # Slack alert after this many consecutive failures
_GEX_INTERVAL   = 900    # GEX refresh cadence in seconds (15 min, RTH only)


def main():
    # ── P2-3: Singleton — only one live writer may run at a time ─────────────
    # Uses fcntl.flock (same pattern as main.py) so the OS releases the lock
    # automatically if the process dies — no stale lock files to clean up.
    _lockfile = open(LOCK_PATH, "w")
    try:
        fcntl.flock(_lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lockfile.write(str(os.getpid()))
        _lockfile.flush()
    except IOError:
        logger.warning("live_data_writer: another instance already running — exiting.")
        sys.exit(0)  # clean exit so launch_bots.sh restart loop doesn't spin
    # FD_CLOEXEC: consistent with main.py — fd auto-closes on exec, preventing
    # lock inheritance across any future process replacements.
    fcntl.fcntl(_lockfile.fileno(), fcntl.F_SETFD, fcntl.FD_CLOEXEC)
    # ─────────────────────────────────────────────────────────────────────────

    logger.info("Live data writer started — refreshing dashboard every %ds", INTERVAL)
    # Delay first write slightly so main.py finishes its own startup write first
    time.sleep(15)

    _consec_errors  = 0
    _backoff        = INTERVAL   # grows on consecutive failures, resets on success
    _alerted        = False      # send Slack once per streak, not every cycle
    _last_gex_ts    = 0.0        # monotonic timestamp of last GEX refresh

    while True:
        try:
            # Import inside loop so any hot-reload of generate_dashboard works
            from generate_dashboard import generate
            generate()
            # Success — reset error streak
            if _consec_errors > 0:
                logger.info(
                    "Dashboard write recovered after %d consecutive failure(s).",
                    _consec_errors,
                )
            _consec_errors = 0
            _backoff       = INTERVAL
            _alerted       = False

            # GEX refresh — every 15 min, RTH only, non-fatal if gex.py not yet deployed
            _now_mono = time.monotonic()
            if _now_mono - _last_gex_ts >= _GEX_INTERVAL:
                try:
                    from data.gex import refresh_gex
                    from zoneinfo import ZoneInfo
                    from datetime import datetime
                    _et   = datetime.now(ZoneInfo("America/New_York"))
                    _mins = _et.hour * 60 + _et.minute
                    if _et.weekday() < 5 and (9 * 60 + 30) <= _mins < (16 * 60):
                        refresh_gex()
                        logger.info("GEX refresh complete.")
                    _last_gex_ts = _now_mono
                except ImportError:
                    logger.debug("data/gex.py not yet available — skipping GEX refresh.")
                except Exception as _gex_e:
                    logger.warning("GEX refresh failed (non-fatal): %s", _gex_e)

        except Exception as e:
            _consec_errors += 1
            # LDW-4: transient network errors (SSL/DNS/connection reset) get a flat
            # INTERVAL retry — no backoff compounding for conditions that self-resolve
            # in seconds. Only logic/import failures compound exponentially.
            _is_network = isinstance(e, (
                ssl.SSLError, socket.timeout, socket.gaierror,
                urllib.error.URLError, ConnectionResetError, TimeoutError,
            ))
            if _is_network:
                _backoff = INTERVAL
                _err_kind = "network"
            else:
                _backoff = min(_backoff * 2, _MAX_BACKOFF)
                _err_kind = "logic"
            logger.error(
                "Dashboard write failed [%s] (consecutive #%d, retry in %ds): %s",
                _err_kind, _consec_errors, _backoff, e, exc_info=True,
            )
            # Single Slack alert once the streak crosses the threshold
            if _consec_errors >= _ALERT_AFTER and not _alerted:
                try:
                    from alerts import send_slack as _sl
                    _sl(
                        f":warning: *Live data writer: {_consec_errors} consecutive "
                        f"dashboard failures*\nLast error: `{str(e)[:200]}`\n"
                        f"Dashboard may be stale. Check `logs/live_writer.log`."
                    )
                    _alerted = True
                except Exception as _sl_e:
                    logger.debug(
                        "live_data_writer: Slack alert failed (non-fatal): %s",
                        _sl_e,
                    )

        time.sleep(_backoff)


if __name__ == "__main__":
    main()
