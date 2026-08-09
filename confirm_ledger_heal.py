#!/usr/bin/env python3
# ruff: noqa: E501
"""confirm_ledger_heal.py — OPERATOR confirmation for a protected-floor DOWN-heal.

The ownership ledger's never-sell floor REFUSES to shrink a protected (qhm/forever6) tier by
default — a legitimate manual close and a catastrophic breach are automation-indistinguishable,
so the irreversible heal-down direction requires an EXPLICIT human confirmation. When YOU
legitimately reduced a quarterly/forever hold (a manual partial close), run this to authorize the
next ledger_sync to heal that ONE symbol/tier DOWN to the real broker qty. One-shot, expiring
(2h), exact-match, snapshot-verified.

Usage:  python3 confirm_ledger_heal.py SYMBOL TIER TARGET_QTY
        e.g.  python3 confirm_ledger_heal.py NVDA qhm 2
"""
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_CONF = _ROOT / "data" / "state" / "ledger_heal_confirmations.json"
_TIERS = ("qhm", "forever6")
_EPS = 1e-6


def _load_env() -> None:
    envp = _ROOT / ".env"
    if not envp.exists():
        return
    for line in envp.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[2] not in _TIERS:
        print("usage: confirm_ledger_heal.py SYMBOL {%s} TARGET_QTY" % "|".join(_TIERS))
        return 2
    sym = sys.argv[1].upper()
    tier = sys.argv[2]
    try:
        target = float(sys.argv[3])
    except ValueError:
        print("TARGET_QTY must be a number")
        return 2
    if target < 0:
        print("TARGET_QTY must be >= 0")
        return 2

    _load_env()
    from reporting.pnl_ledger import fetch_positions
    positions = fetch_positions()
    net = 0.0
    found = False
    for p in (positions or []):
        if p.get("symbol") == sym:
            net = float(p.get("qty", 0.0) or 0.0)
            found = True
            break
    if not found:
        net = 0.0  # symbol absent → fully closed → net 0

    if net < target - _EPS:
        print(f"REFUSED: live broker net {net} < target {target} for {sym} — target not achievable.")
        return 1

    confs: dict = {}
    try:
        if _CONF.exists():
            d = json.loads(_CONF.read_text())
            confs = d if isinstance(d, dict) else {}
    except Exception:
        confs = {}
    confs[f"{sym}/{tier}"] = {
        "target_qty": target,
        "net_at_confirm": net,
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
    print(f"CONFIRMED heal-down {sym}/{tier} -> {target:g} (live net={net:g}). Valid 2h, one-shot. "
          "Next ledger_sync applies it only if the snapshot still matches, then consumes it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
