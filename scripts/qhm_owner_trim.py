# ruff: noqa: E501  — dense rationale lines run long (project convention)
"""scripts/qhm_owner_trim.py — arm, cancel or show an OWNER TRIM of a quarterly hold (Claude 2026-10-14).

The bot (execution/quarterly_hold_manager._maybe_owner_trim) executes an armed request during market hours: when the
last trade reaches --trigger, or at market on/after --deadline. It always leaves at least one share with its stop.
This script only writes the request file; it never talks to the broker.

  python3 scripts/qhm_owner_trim.py arm LLY --qty 1 --trigger 1222 --deadline 2026-10-28
  python3 scripts/qhm_owner_trim.py cancel LLY
  python3 scripts/qhm_owner_trim.py show
"""
import argparse
import fcntl
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "data" / "state" / "qhm_owner_trims.json"
HOLDS = ROOT / "data" / "state" / "quarterly_holds.json"
PT = ZoneInfo("America/Los_Angeles")


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    d = json.loads(path.read_text())
    if not isinstance(d, dict):
        raise SystemExit(f"{path} is not a JSON object — fix it by hand first")
    return d


def _write(d: dict) -> None:
    REQ.parent.mkdir(parents=True, exist_ok=True)
    tmp = REQ.with_suffix(f".json.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, REQ)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("arm", "cancel", "show"))
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("--qty", type=int)
    ap.add_argument("--trigger", type=float, help="sell when the last trade is at or above this price")
    ap.add_argument("--deadline", help="YYYY-MM-DD: sell at market on/after this date if not triggered")
    a = ap.parse_args()
    REQ.parent.mkdir(parents=True, exist_ok=True)
    with open(str(REQ) + ".lock", "w") as lk:      # the same lock the bot takes when it updates a request
        fcntl.flock(lk, fcntl.LOCK_EX)
        return _run(ap, a)


def _run(ap, a) -> int:
    reqs = _load(REQ)
    if a.action == "show":
        print(json.dumps(reqs, indent=2) if reqs else "no owner trims")
        return 0
    if not a.symbol:
        ap.error("symbol required")
    sym = a.symbol.upper()
    now = datetime.now(PT).isoformat()
    if a.action == "cancel":
        cur = reqs.get(sym) or {}
        if cur.get("status") == "reserved":
            raise SystemExit(f"{sym} trim is 'reserved' (mid-execution or stuck) — verify in Alpaca before editing by hand")
        reqs[sym] = {**cur, "status": "cancelled", "updated_at": now}
        _write(reqs)
        print(f"{sym} owner trim cancelled")
        return 0
    if a.qty is None or a.deadline is None:
        ap.error("arm needs --qty and --deadline (--trigger optional)")
    if a.qty < 1:
        ap.error("--qty must be at least 1")
    dl = date.fromisoformat(a.deadline)
    if dl < datetime.now(PT).date():
        ap.error(f"--deadline {dl} is in the past — it would sell at market on the next cycle")
    hold = _load(HOLDS).get(sym)
    if not isinstance(hold, dict) or hold.get("state") != "ACTIVE":
        raise SystemExit(f"{sym} is not an ACTIVE quarterly hold ({(hold or {}).get('state')}) — nothing armed")
    held = int(hold.get("qty_filled") or 0)
    if a.qty >= held:
        raise SystemExit(f"{sym}: --qty {a.qty} must leave at least one of the {held} shares held")
    if (reqs.get(sym) or {}).get("status") == "reserved":
        raise SystemExit(f"{sym} trim is 'reserved' (mid-execution or stuck) — verify in Alpaca first")
    reqs[sym] = {"qty": a.qty, "trigger_price": a.trigger, "deadline": dl.isoformat(), "status": "armed",
                 "entry_day": hold.get("entry_day"), "armed_at": now, "updated_at": now}
    _write(reqs)
    print(f"{sym} owner trim ARMED: sell {a.qty} of {held} when last >= {a.trigger}, else at market on/after {dl}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
