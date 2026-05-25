"""
monitoring/watchdog.py
Cycle watchdog thread and heartbeat — extracted from main.py Phase 2.

Owns:
  - _last_cycle_complete   epoch timestamp of last completed run_cycle()
  - _last_cycle_lock       threading.Lock guarding the timestamp (BUG-W2)
  - touch_cycle_ts()       thread-safe stamp called at every run_cycle() exit
  - start_watchdog()       arms the daemon watchdog thread

Watchdog behavior:
  - Arms 3 minutes after start (first cycle expected within that window)
  - Resets baseline at overnight→RTH transition (prevents false positive on
    the 30-min overnight sleep)
  - If no cycle completes within WATCHDOG_TIMEOUT during RTH: fires Slack alert
    then os.execv() restarts the process
  - Exception in watchdog loop is logged and swallowed — watchdog must never
    crash the bot process

Thread-safety: _last_cycle_complete is only read/written under _last_cycle_lock
(BUG-W2 fix). touch_cycle_ts() is safe to call from any thread.
"""

import logging
import os
import sys
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

WATCHDOG_TIMEOUT = 12 * 60   # 12 minutes — 2.4× the 5-min scan interval

# ---------------------------------------------------------------------------
# Shared cycle timestamp (single writer: main thread via touch_cycle_ts)
# ---------------------------------------------------------------------------

_last_cycle_complete: float = 0.0
_last_cycle_lock = threading.Lock()   # BUG-W2


def touch_cycle_ts() -> None:
    """Thread-safe stamp of _last_cycle_complete.

    Call at every run_cycle() exit point (normal, early-return, error).
    Replaces _touch_cycle_ts() from main.py.
    """
    global _last_cycle_complete
    with _last_cycle_lock:
        _last_cycle_complete = time.time()


# ---------------------------------------------------------------------------
# Watchdog thread
# ---------------------------------------------------------------------------

def _watchdog_loop(lockfile_path: str) -> None:
    """Core watchdog loop. Runs as a daemon thread."""
    threading.current_thread().name = "CycleWatchdog"
    # Give bot 3 minutes to complete its first cycle before arming
    time.sleep(180)
    global _last_cycle_complete
    with _last_cycle_lock:
        _last_cycle_complete = time.time()   # arm baseline after delay
    _prev_rth = False

    while True:
        time.sleep(60)
        try:
            _now_et  = datetime.now(ET)
            _mins_et = _now_et.hour * 60 + _now_et.minute
            _is_rth  = (9 * 60 + 30) <= _mins_et < (16 * 60)

            # Overnight→RTH transition: reset baseline so the 30-min sleep
            # between overnight scans is not counted as a hang.
            if _is_rth and not _prev_rth:
                with _last_cycle_lock:
                    _last_cycle_complete = time.time()
                logger.debug("Watchdog: overnight→RTH transition — baseline reset")
            _prev_rth = _is_rth

            if not _is_rth:
                continue

            with _last_cycle_lock:
                _elapsed = time.time() - _last_cycle_complete

            if _elapsed > WATCHDOG_TIMEOUT:
                _elapsed_min = int(_elapsed / 60)
                logger.critical(
                    f"WATCHDOG: No completed cycle in {_elapsed_min}m during RTH — "
                    f"bot is hung. Firing alert and restarting."
                )
                try:
                    from alerts import send_slack
                    send_slack(
                        f":skull: *BOT HUNG — AUTO-RESTARTING* :skull:\n"
                        f"No scan cycle completed in *{_elapsed_min} minutes* during RTH.\n"
                        f"Likely cause: yfinance network hang or deadlock.\n"
                        f"Watchdog is restarting the bot now. "
                        f"Open positions are unmonitored until restart completes."
                    )
                except Exception as _alert_e:
                    logger.warning(
                        f"Watchdog Slack alert failed (continuing restart): {_alert_e}"
                    )
                try:
                    os.unlink(lockfile_path)
                except Exception as _lock_e:
                    logger.warning(
                        f"Watchdog lockfile unlink failed (stale lock possible): {_lock_e}"
                    )
                os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))
                os.execv(sys.executable, [sys.executable] + sys.argv)

        except Exception as _e:
            logger.warning(f"Watchdog check error (non-fatal): {_e}")


def start_watchdog(lockfile_path: str) -> threading.Thread:
    """Start the cycle watchdog daemon thread and return it.

    Args:
        lockfile_path: Path to the process lockfile. Unlinked before os.execv()
                       restart so the new process can acquire a fresh lock.
    """
    t = threading.Thread(
        target=_watchdog_loop,
        args=(lockfile_path,),
        daemon=True,
    )
    t.start()
    logger.info(f"Cycle watchdog armed: RTH hang threshold = {WATCHDOG_TIMEOUT // 60} min")
    return t
