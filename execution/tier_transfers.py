# ruff: noqa: E501
"""
execution/tier_transfers.py — durable journal of share TRANSFERS between the bot's liquidatable tiers.

WHY (CEO 2026-10-10): the ownership ledger (execution/ownership_guard.sync_ledger) attributes every Alpaca fill to the
tier in its order tag (DT- = Day, IN- = Swing). A lot that changes hands without a trade — a Day lot promoted to the
Swing tier at the close (execution/day_promotion.py), or a Day lot the Swing tier adopts as an orphan — keeps its DT-
buy, so the ledger kept it as Day and later booked the Swing tier's sell against Swing, leaving crossed rows (AAPL
day +4 / swing -4; EWY day -1 while the Swing book holds the short). This journal records each such hand-over; the
ledger replay applies it at its timestamp, moving exactly that many shares between the two tiers.

CONTRACT
  * Only between the liquidatable tiers "daytrade" and "intraday" (legacy ledger keys for Day and Swing). The
    protected tiers (qhm / forever6) are never touched, so the never-shrink-a-protected-floor guard is unaffected.
  * qty is SIGNED: +n moves n long shares, -n moves an n-share short.
  * One record per `ref` (idempotent: a repeated record with the same ref is a no-op).
  * The ledger applies a transfer only when the source tier holds at least that many same-signed shares at that
    moment, so a transfer can never create shares.

FILE: data/state/tier_transfers.json — {ref: record}, atomic tmp->replace under an exclusive flock (two processes
write it: the Day runner and the main bot). Data tier: none (local state only). Never raises from record_transfer.
"""
from __future__ import annotations

import fcntl
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_PATH = Path(__file__).resolve().parent.parent / "data" / "state" / "tier_transfers.json"
_TIERS = ("daytrade", "intraday")


def _ts_key(dt: datetime) -> str:
    """UTC ISO string in the Alpaca fill format (YYYY-MM-DDTHH:MM:SS.ffffffZ) so the ledger replay orders transfers
    and fills on one key."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _read() -> "dict | None":
    """{ref: record}; {} when absent; None when present but unreadable."""
    try:
        if not _PATH.exists():
            return {}
        d = json.loads(_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.error("tier transfers unreadable: %s", e)
        return None


def _valid(r: object) -> bool:
    try:
        assert isinstance(r, dict)
        q = float(r["qty"])
        px = float(r["price"])
        datetime.fromisoformat(str(r["ts_utc"]).replace("Z", "+00:00"))
        return (bool(r.get("symbol")) and r.get("from_tier") in _TIERS and r.get("to_tier") in _TIERS
                and r["from_tier"] != r["to_tier"] and math.isfinite(q) and abs(q) > 0
                and math.isfinite(px) and px > 0)
    except Exception:  # noqa: BLE001
        return False


def record_transfer(ref: str, symbol: str, qty: float, from_tier: str, to_tier: str, price: float,
                    when: "datetime | None" = None, source: str = "") -> bool:
    """Record one hand-over (idempotent by `ref`). True when the record exists after the call. Never raises."""
    try:
        rec = {"symbol": str(symbol).upper(), "qty": float(qty), "from_tier": from_tier, "to_tier": to_tier,
               "price": round(float(price), 4), "ts_utc": _ts_key(when or datetime.now(timezone.utc)),
               "source": source, "recorded_utc": _ts_key(datetime.now(timezone.utc))}
        if not ref or not _valid(rec):
            logger.error("tier transfer %r rejected (invalid): %s", ref, rec)
            return False
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(str(_PATH) + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                cur = _read()
                if cur is None:
                    return False                      # never overwrite an unreadable journal
                if ref in cur:
                    return True
                cur[ref] = rec
                tmp = _PATH.with_suffix(f".tmp{os.getpid()}")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(cur, f, indent=1)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, _PATH)
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)
        logger.warning("tier transfer recorded: %s %s %+g %s->%s @ %.2f (%s)", ref, rec["symbol"], rec["qty"],
                       from_tier, to_tier, rec["price"], source)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("tier transfer %r not recorded: %s", ref, e)
        return False


def load_transfers() -> "list | None":
    """Every valid transfer record (with its ref), oldest first; [] when none; None when the journal is unreadable
    (the ledger maintainer then skips the pass, leaving the ledger at its last-good state). Invalid rows are logged
    and skipped."""
    cur = _read()
    if cur is None:
        return None
    out = []
    for ref, r in cur.items():
        if _valid(r):
            out.append({**r, "ref": ref})
        else:
            logger.error("tier transfer %r invalid — ignored: %s", ref, r)
    return sorted(out, key=lambda r: r["ts_utc"])
