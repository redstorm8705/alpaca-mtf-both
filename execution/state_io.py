"""
execution/state_io.py — shared low-level state I/O primitives.

Leaf module (no intra-project imports) so both execution/fifo_pnl.py and
execution/portfolio_tracker.py can depend on it without a circular import.
Extracted verbatim from portfolio_tracker.py in M1 (2026-07-06), ZERO logic
change. Dependency graph: portfolio_tracker → fifo_pnl → state_io (a DAG).

Holds: _ET/_PT timezones, the certifi-backed _SSL_CTX, the numpy-safe
_BotEncoder JSON encoder, and the _atomic_write helper.
"""

import json
import os
import shutil
import ssl
import tempfile
import logging
import re
from datetime import datetime
from pathlib import Path
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

# ── Tolerant ISO-8601 parser (RC-4 datetime fix, board+Gro+GAI 2026-07-19) ────
# Python 3.10's datetime.fromisoformat rejects a trailing 'Z' and any fractional
# seconds that is not exactly 3 or 6 digits. Alpaca `filled_at` values (which can be
# stored as a trade's exit_time) carry both. A raw parse there fails and the P&L
# reconciliation silently skips the trade (permanent corruption / re-queue loop).
_FRAC_RE = re.compile(r"\.(\d+)")


def _iso_to_dt(ts):
    """Parse an ISO-8601 timestamp tolerant of Alpaca's variable-length fractional
    seconds (1-9 digits) and the 'Z' suffix. Pads/truncates the fraction to exactly 6
    digits and maps 'Z'->'+00:00'. Returns an AWARE datetime when the input carries an
    offset/'Z', a NAIVE datetime for an offset-less string, or None on genuine failure.
    NEVER raises — callers decide how to degrade. Kept here (not imported from
    reporting/pnl_ledger, whose identical helper would be a circular import: pnl_ledger
    imports execution.quarterly_hold_manager)."""
    if not ts:
        return None
    try:
        _t = ts.strip().replace("Z", "+00:00")
        m = _FRAC_RE.search(_t)
        if m:
            _frac6 = (m.group(1) + "000000")[:6]
            _t = _t[:m.start() + 1] + _frac6 + _t[m.end():]
        return datetime.fromisoformat(_t)
    except (ValueError, TypeError, AttributeError):
        return None


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

# Project root (this file lives in execution/, so parent.parent = repo root) and
# the FIFO lot-state path. Single source of truth so fifo_pnl (and any future
# consumer) resolve open_lots_prior_day.json identically — closes the two-module
# _ROOT re-derivation flagged by GAI + the execution-risk board in M1 review.
_ROOT            = Path(__file__).parent.parent.resolve()   # alpaca-mtf-bot_FINAL/
_LOTS_STATE_FILE = _ROOT / "data" / "state" / "open_lots_prior_day.json"


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
