#!/usr/bin/env python3
# ruff: noqa: E501
"""EOD-FRIDAY-1117 one-time repair (owner-approved 2026-11-17; logs/design_records/eod_friday_2026-11-17.md).

Friday 2026-11-13's trend-night QQQM buy (2 @ 279.91 at the close) never reached the tracker's end-of-day lot pass, so
Monday 2026-11-16's sale of those 2 shares was stored as two synthetic SHORT lots in data/state/open_lots_prior_day.json
(1 @ 274.69 and 1 @ 274.79). Left there, the next QQQM buy would be booked as covering them.

This script removes exactly those two lots and nothing else.
  * Dry run by default: prints what it would change. `--apply` writes.
  * Refuses unless the file's QQQM entry is exactly the two expected short lots (any other content: unchanged).
  * `--apply` refuses while the mtf-bot service is active (the bot rewrites this file on every pass); run it as
    (the `;` restarts the bot even if the repair refuses):
        sudo systemctl stop mtf-bot; python3 scripts/repair_fifo_lots_1117.py --apply; sudo systemctl start mtf-bot
  * Writes a timestamped backup next to the file, then the new content through a temporary file + os.replace.
No broker call is made. Authoritative P&L (the Alpaca fills ledger) is not touched.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOTS_PATH = ROOT / "data" / "state" / "open_lots_prior_day.json"
SYMBOL = "QQQM"
EXPECTED = [{"qty": 1, "price": 274.69, "side": "short"}, {"qty": 1, "price": 274.79, "side": "short"}]


def bot_active() -> bool:
    """True when systemd reports mtf-bot active, or when that cannot be determined (fail closed)."""
    try:
        r = subprocess.run(["systemctl", "is-active", "mtf-bot"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return True
    return r.stdout.strip() != "inactive"


def plan(data: dict) -> "tuple[dict | None, str]":
    """(new content, message). new content is None when the file must not be changed."""
    lots = data.get("open_lots") if isinstance(data, dict) else None
    if not isinstance(lots, dict):
        return None, "REFUSED: the file has no 'open_lots' object; file unchanged"
    if SYMBOL not in lots:
        return None, f"REFUSED: no {SYMBOL} entry in open_lots; nothing to repair"
    if lots[SYMBOL] != EXPECTED:
        return None, f"REFUSED: {SYMBOL} entry is {lots[SYMBOL]!r}, not the expected {EXPECTED!r}; file unchanged"
    new = json.loads(json.dumps(data))
    del new["open_lots"][SYMBOL]
    return new, f"remove {SYMBOL} {EXPECTED!r} from open_lots"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    ap.add_argument("--path", default=str(LOTS_PATH), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    path = Path(args.path)
    if not path.exists():
        print(f"REFUSED: {path} not found; nothing changed", file=sys.stderr)
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"REFUSED: {path} unreadable ({e}); nothing changed", file=sys.stderr)
        return 1
    new, msg = plan(data)
    if new is None:
        print(msg, file=sys.stderr)
        return 1
    if not args.apply:
        print(f"DRY RUN: would {msg} in {path}")
        return 0
    if bot_active():
        print("REFUSED: mtf-bot is active (or its state is unknown); stop it first; nothing changed", file=sys.stderr)
        return 1
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak_repair_1117_{stamp}")
    shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(new, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    print(f"APPLIED: {msg}; backup at {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
