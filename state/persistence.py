# ruff: noqa: E501
"""
state/persistence.py
Atomic JSON write helpers for all engine state files — extracted from main.py Phase 2.

Owns:
  - confirm_gate.json      (entry gate state, cleared daily)
  - bot_status.json        (dashboard feed, written mid-cycle)

Does NOT own (single-domain classes keep their own writes):
  - trade_log.json         → execution/portfolio_tracker.py (PortfolioTracker._atomic_write)
  - tqi_history.json       → execution/kelly.py (KellySizer.append_tqi)
  - hybrid_state.json      → events/news_monitor.py (_persist_macro_risk_window)
  - kill_switch_state.json → execution/risk_manager.py

Atomic write pattern (SIGKILL-safe):
  1. Write to .tmp file via tempfile.mkstemp (same filesystem → rename is atomic)
  2. os.fsync() — flush kernel buffers to disk before rename
  3. os.replace() — atomic on POSIX: file is never partially-written
  4. Exception handler unlinks orphaned .tmp on failure

RC-2 compliance: all paths anchored to _BOT_ROOT (Path(__file__).resolve().parent.parent).
RC-5 compliance: all writes use _atomic_write() — no raw open(path, "w") patterns.
"""

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Path constants — anchored to bot root, never CWD-relative
# ---------------------------------------------------------------------------

_BOT_ROOT  = Path(__file__).resolve().parent.parent
_LOGS_DIR  = _BOT_ROOT / "logs"

CONFIRM_GATE_FILE  = _LOGS_DIR / "confirm_gate.json"
BOT_STATUS_FILE    = _LOGS_DIR / "bot_status.json"


# ---------------------------------------------------------------------------
# Core atomic write primitive
# ---------------------------------------------------------------------------

def _atomic_write(filepath: Path, data: dict) -> None:
    """Write JSON atomically via tempfile + fsync + os.replace.

    Guarantees the file is never observed in a partially-written state,
    even under SIGKILL mid-write.
    """
    filepath.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(filepath.parent), suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError as _fd_e:
                logger.debug("[persistence] fd close failed during cleanup (non-critical): %s", _fd_e)
            raise
        os.replace(str(tmp_path), str(filepath))
    except Exception as e:
        logger.warning(f"[persistence] Atomic write failed for {filepath}: {e}")
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError as _unl_e:
                logger.debug("[persistence] tmp unlink failed (orphan .tmp may remain): %s", _unl_e)


# ---------------------------------------------------------------------------
# confirm_gate.json
# ---------------------------------------------------------------------------

def write_confirm_gate(entry_confirm_buffer: dict, conviction_streak: dict) -> None:
    """Persist entry gate state atomically.

    Written after every RC-8 clear and at end of each execute_entries() cycle.
    Deleted at daily reset (clear_confirm_gate).
    Date-stamped with ET date so stale gates are rejected on next-day restore.
    """
    _atomic_write(CONFIRM_GATE_FILE, {
        "entry_confirm_buffer": entry_confirm_buffer,
        "conviction_streak":    conviction_streak,
        "date":                 str(datetime.now(ET).date()),
    })


def read_confirm_gate() -> dict | None:
    """Read confirm gate JSON. Returns None if absent, unreadable, or from prior day."""
    try:
        if CONFIRM_GATE_FILE.exists():
            with open(CONFIRM_GATE_FILE) as f:
                data = json.load(f)
            if data.get("date") == str(datetime.now(ET).date()):
                return data
    except Exception as e:
        logger.debug(f"[persistence] confirm_gate read failed (non-critical): {e}")
    return None


def clear_confirm_gate() -> None:
    """Delete confirm_gate.json at daily reset. Stale gates must not survive day boundary."""
    try:
        CONFIRM_GATE_FILE.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(
            f"[daily_reset] Failed to delete confirm_gate.json — stale gate may persist: {e}"
        )


# ---------------------------------------------------------------------------
# bot_status.json
# ---------------------------------------------------------------------------

def write_bot_status(status: dict) -> None:
    """Persist bot_status.json for dashboard feed. Atomic write with fsync."""
    _atomic_write(BOT_STATUS_FILE, status)
