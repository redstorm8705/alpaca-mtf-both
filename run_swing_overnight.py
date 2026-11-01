# ruff: noqa: E501  — dense rationale lines run long (project convention)
"""run_swing_overnight.py — cron runner for the Swing tier's `trend_night` sub-strategy (SWING-OVERNIGHT-1030).

Cron (OCI): every minute on weekdays, e.g.  `* * * * 1-5 cd <repo> && python3 run_swing_overnight.py`.
A separate process — never the run_cycle thread. Each run:
  0. takes a non-blocking flock singleton (a run still polling inside an order window keeps it; the next minute's run
     sees it held and exits at once — "another run holds the lock" also blocks entries);
  1. builds execution.swing_overnight.TrendNight and calls run(): one reconcile pass, then — only inside an order window
     (close − 20 min … close + 10 min, open − 10 min … open + 10 min, or an exit retry) — keeps polling the broker every
     SWING_TREND_NIGHT_POLL_SECONDS until every trend_night order (IN-QQQM-...-tn<step><n>) is terminal and the stop
     covers the held shares.
     Outside every window with nothing held it is a no-op (no broker order calls).
Logs: logs/swing_overnight.log (+ trade_events.jsonl via trade_logger).
"""
import fcntl
import logging
import os
import stat
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")

_LOCK = "/tmp/swing_trend_night_runner.lock"          # POSIX singleton lock (/tmp allowed for locks)
_LOG = _ROOT / "logs" / "swing_overnight.log"


def _setup_logging() -> None:
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.FileHandler(_LOG), logging.StreamHandler()])


def main(lock_path: str = _LOCK) -> int:
    _setup_logging()
    log = logging.getLogger("run_swing_overnight")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, stat.S_IRUSR | stat.S_IWUSR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("trend_night: another run holds the lock — exiting (it is still polling its window)")
            return 0
        from execution.swing_overnight import TrendNight
        try:
            engine = TrendNight()
        except Exception as e:  # noqa: BLE001 — never a traceback storm from cron; page and retry next minute
            log.critical("trend_night: engine start failed: %r", e)
            try:
                from alerts import send_slack
                send_slack(f"🚨 SWING trend_night: runner could not start ({e!r}) — retrying next minute")
            except Exception as pe:  # noqa: BLE001
                log.error("trend_night: page not sent: %s", pe)
            return 1
        return engine.run()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
