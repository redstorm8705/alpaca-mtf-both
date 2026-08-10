#!/usr/bin/env python3
# ruff: noqa: E501
"""confirm_drift_correction.py — OPERATOR confirmation for ONE mid-session tracker↔broker
drift correction (Option C1, Rafael 2026-08-09; BGG unanimous: human-confirmed only, NO
unattended set-mutation).

The drift detector SURFACES a persistent tracker↔broker mismatch (a phantom the tracker holds
that the broker doesn't, a wrong direction, a qty mismatch, a corrupted entry) but NEVER corrects
it on its own — every unattended correction of the position SET has caused an incident (HOOD
false-drop on a stale read, RIVN inverted-short adoption). When YOU have verified a specific
detected drift is real, run this to authorize the next cycle to apply THAT ONE correction. The
authorization is one-shot, 2h-expiring, and SNAPSHOT-VERIFIED: it applies only if the live tracker
AND broker quantities still exactly match what you confirmed — if either moved even by one share,
it fails closed, consumes the token, and re-pages (a moved snapshot means the situation changed).

The detector Slacks you the exact command to run. Usage:
  python3 confirm_drift_correction.py SYMBOL DRIFT_TYPE TRACKER_QTY BROKER_QTY
    e.g.  python3 confirm_drift_correction.py SNOW phantom_tracker 1 0

DRIFT_TYPE ∈ {phantom_tracker, phantom_broker, direction_mismatch, qty_mismatch, entry_corrupt}
"""
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_CONF = _ROOT / "data" / "state" / "drift_correction_confirmations.json"
_TRADE_LOG = _ROOT / "trade_log.json"
_TYPES = ("phantom_tracker", "phantom_broker", "direction_mismatch", "qty_mismatch", "entry_corrupt")


def _load_env() -> None:
    envp = _ROOT / ".env"
    if not envp.exists():
        return
    for line in envp.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _tracker_qty(sym: str) -> int:
    """Live tracker qty for sym from trade_log.json open book (0 if not held). Signed by direction
    only via abs — this confirms the magnitude the operator saw; direction is a separate field."""
    try:
        d = json.loads(_TRADE_LOG.read_text())
    except Exception:
        return 0
    for t in (d.get("open", []) if isinstance(d, dict) else []):
        if t.get("symbol") == sym and t.get("status") != "closed":
            q = t.get("qty_remaining")
            if q is None:
                q = t.get("qty")
            try:
                return int(abs(float(q or 0)))
            except (TypeError, ValueError):
                return 0
    return 0


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[2] not in _TYPES:
        print("usage: confirm_drift_correction.py SYMBOL {%s} TRACKER_QTY BROKER_QTY"
              % "|".join(_TYPES))
        return 2
    sym = sys.argv[1].upper()
    dtype = sys.argv[2]
    try:
        exp_tracker = int(sys.argv[3])
        exp_broker = int(sys.argv[4])
    except ValueError:
        print("TRACKER_QTY and BROKER_QTY must be integers")
        return 2

    _load_env()
    from reporting.pnl_ledger import fetch_positions
    positions = fetch_positions()
    broker = 0
    for p in (positions or []):
        if p.get("symbol") == sym:
            try:
                broker = int(abs(float(p.get("qty", 0.0) or 0.0)))
            except (TypeError, ValueError):
                broker = 0
            break
    tracker = _tracker_qty(sym)

    # SNAPSHOT VERIFY: the live tracker + broker must STILL match what the operator is confirming.
    # If either moved, the situation changed — refuse (fail-closed) so a stale confirm can't apply.
    if tracker != exp_tracker or broker != exp_broker:
        print(f"REFUSED: live snapshot moved since you looked — {sym} tracker={tracker} broker={broker}, "
              f"you confirmed tracker={exp_tracker} broker={exp_broker}. Re-check the current drift alert.")
        return 1
    # Sanity: the confirmed drift_type must be consistent with the snapshot.
    if dtype == "phantom_tracker" and not (tracker > 0 and broker == 0):
        print(f"REFUSED: {sym} is not a phantom_tracker (tracker={tracker}, broker={broker}).")
        return 1
    if dtype == "phantom_broker" and not (broker > 0 and tracker == 0):
        print(f"REFUSED: {sym} is not a phantom_broker (tracker={tracker}, broker={broker}).")
        return 1
    if dtype == "qty_mismatch" and not (tracker > 0 and broker > 0 and tracker != broker):
        print(f"REFUSED: {sym} is not a qty_mismatch (tracker={tracker}, broker={broker}).")
        return 1

    confs: dict = {}
    try:
        if _CONF.exists():
            d = json.loads(_CONF.read_text())
            confs = d if isinstance(d, dict) else {}
    except Exception:
        confs = {}
    confs[f"{sym}/{dtype}"] = {
        "symbol": sym,
        "drift_type": dtype,
        "tracker_at_confirm": tracker,
        "broker_at_confirm": broker,
        "confirmed_ts": time.time(),
        "confirmed_by": os.environ.get("USER", "operator"),
    }
    _CONF.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONF.with_suffix(f".json.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(confs, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(_CONF)
    print(f"CONFIRMED drift correction {sym}/{dtype} (tracker={tracker} broker={broker}). "
          "Valid 2h, one-shot. The next cycle applies it ONLY if the snapshot still matches, then consumes it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
