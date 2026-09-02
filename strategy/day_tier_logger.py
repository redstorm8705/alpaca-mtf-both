# ruff: noqa: E501  — dense rationale/docstring comments run long (project convention)
"""
strategy/day_tier_logger.py — durable, restart-safe decision + price-trajectory log for the
DAY-TRADE tier (Rafael keystone 2026-09-01/09-02; board data-integrity seat design 2026-09-02).

WHY (Rafael, stated twice, non-negotiable): the day-tier ships LIVE (paper) with NO post-hoc
shadow safety net, so every decision, entry, exit, fill, price, size, and realized P&L MUST be
written to disk IMMEDIATELY and COMPLETELY — append + flush + fsync — surviving any restart, with
ZERO weekend gaps. This is the §4 decision-explainability / §7b.4 hedge-fund-grade 100% red-day
audit substrate for the most volatile tier. Plus Rafael's amended PRICE-TRAJECTORY spec: for each
day-trade, capture (1) a price snapshot at entry execution (fill price + market price), (2) the
price at EACH 30-min interval while the position is OPEN (the price PATH), and (3) a price
snapshot at exit — so the weekend analysis can reconstruct exactly what price was doing during,
and after, every executed trade, and find areas of optimization.

DEDICATED FILE logs/day_tier_events.jsonl (NOT the shared trade_events.jsonl): the 2-3 min
execution runner is the SINGLE OWNING WRITER here, so O_APPEND stays atomic (lines may exceed the
4096-byte PIPE_BUF) and the fsync-per-event durability contract can never collide with the 5-min
main scan / portfolio_tracker writers on the shared file (data-integrity seat: cross-cadence,
multi-writer append contention + torn-line risk). The canonical minimal lifecycle event still goes
to trade_events.jsonl (P&L system of record, §4) via trade_logger — the ORDER MODULE dual-writes;
this module owns only the enrichment/trajectory file. Both are keyed by the same trade_id so they
join offline.

DURABILITY (data-integrity seat — the in-repo verified pattern, mirrors run_day_tier_shadow.py
_append_log): every write is `f.write(...) ; f.flush() ; os.fsync(f.fileno())` inside ONE
`with open(path, "a")`. O_APPEND makes each line atomic at the kernel level; fsync forces the data
to stable storage before the call returns. fsync-per-event is SAFE on the 2-3 min loop (single-
digit events/tick, ms-scale) and is placed by the caller AFTER the broker round-trip + stop
placement, so it is never trade-blocking (log-after-stop: the worst case is a live stop with a
missing log line, which exit reconciliation backfills — never a delayed stop).

RESTART-SAFE (the one weekend-gap vector the seat named): the 30-min price sampler MUST be
STATELESS — derive the open set every tick by REPLAYING this log (a trade_id with an entry_fill
and no exit_fill is open), never from in-memory state. open_trades_from_log() does exactly that,
so a mid-trade restart (OCI cron cycle, OOM, deploy) re-derives the open set with zero gaps and
the price PATH is never silently holed. `seq` on each price_sample makes a missing sample
detectable.

KEY: trade_id = the order's client_order_id (execution.ownership_guard.make_coid, "DT-..."),
minted once per order and reused across retries. order_id is persisted on both fills so the
coid<->order_id fill-attribution join (Alpaca FILL activities carry only order_id) is
reconstructable offline.

This module is pure logging (writes logs/ only) — no order placement, no sizing, no entry/exit
decision — and imports nothing from execution/, so it is independently testable in isolation.
"""

from __future__ import annotations  # PEP-604 (X | None) hints stay lazy → safe on Python 3.9+

import functools
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

PT = ZoneInfo("America/Los_Angeles")
_JSONL = Path(__file__).resolve().parent.parent / "logs" / "day_tier_events.jsonl"
SCHEMA_V = 1

# Write-failure operator alert throttle — mirrors trade_logger.py. A persistent write failure
# would fire on ~every event, so escalate LOUDLY (ERROR + throttled Slack) at most once per
# window. In-memory (not a state file): this runs inside the disk-write failure handler, so a
# throttle-state file write could itself fail. Durability is the whole point of this module, so a
# silent write outage is exactly the "partial weekend data" failure Rafael forbade — it must page.
_WRITE_FAIL_ALERT_THROTTLE_S = 3600.0
_last_write_fail_alert = 0.0
# One-time parent-dir fsync guard (guards the directory ENTRY against power loss on first create).
_dir_fsynced = False


def _json_default(o: Any) -> Any:
    """Coerce a value json.dumps cannot natively serialize (e.g. a numpy scalar), so one odd type
    can never abort an event write. Same contract as trade_logger._json_default: the result is
    ALWAYS JSON-native, so json.dumps can never re-raise on what this returns. Log path only —
    never a P&L or trade-decision value — so a faithful coercion here cannot mask a loss."""
    item = getattr(o, "item", None)
    if callable(item):
        try:
            v = item()
            if v is None or isinstance(v, (bool, int, float, str)):
                return v
        except Exception:
            pass
    return str(o)


def _alert_write_fail(where: str, err: Exception) -> None:
    """Throttled LOUD escalation of a durable-log write failure (bounded Slack spam + bounded
    blocking). Never raises — a failed/blocked alert must not crash or stall the caller."""
    logger.error(f"day_tier_events.jsonl write FAILED ({where}): {err}")
    global _last_write_fail_alert
    now = time.monotonic()
    if now - _last_write_fail_alert >= _WRITE_FAIL_ALERT_THROTTLE_S:
        _last_write_fail_alert = now  # stamp BEFORE send: caps blocking even if Slack is down
        try:
            from alerts import send_slack
            send_slack(
                "⚠️ WARNING — day_tier_events.jsonl WRITE FAILING\n"
                f"Last error ({where}): {err}\n"
                "The day-tier durable decision/price-trajectory log is not recording; the "
                "weekend red-day audit will be incomplete until fixed. Position stops are "
                "UNAFFECTED (this is the enrichment log, not the P&L system of record)."
            )
        except Exception as alert_e:  # pragma: no cover - alert best-effort
            logger.error(f"day_tier_logger: write-fail alert itself failed: {alert_e}")


def _durable_append(records: list[dict]) -> bool:
    """Append one or more JSON-line records to logs/day_tier_events.jsonl with a single
    flush+fsync covering the whole batch (data-integrity seat: batch the 30-min multi-symbol
    samples into ONE fsync). Returns True on success, False on failure (never raises — the caller
    is the trading runner and a log failure must never stall or crash it; the failure is paged).

    A batch write is atomic at the record level via O_APPEND; on a crash mid-batch the reader
    tolerates a torn trailing line (read_events skips it). fsync guarantees everything BEFORE the
    crash is durable."""
    if not records:
        return True
    global _dir_fsynced
    try:
        os.makedirs(_JSONL.parent, exist_ok=True)
        # TORN-LINE RECOVERY (cold-2nd #4, 2026-09-02): a kill-9 mid-write can leave a partial line
        # with no trailing newline; without this, the next record concatenates onto it into one
        # unparseable line, so the FIRST good post-crash record would also be lost. Every complete
        # record we write ends with "\n", so a file that does NOT end in "\n" can only be mid a torn
        # line — writing a leading "\n" closes that torn line onto its own (skippable) line and keeps
        # the new record parseable. Single-owner file, so this never splits a good record.
        _lead = ""
        try:
            if _JSONL.exists() and _JSONL.stat().st_size > 0:
                with open(_JSONL, "rb") as rf:
                    rf.seek(-1, os.SEEK_END)
                    if rf.read(1) != b"\n":
                        _lead = "\n"
        except Exception:
            _lead = ""  # best-effort; never block the write on the newline probe
        with open(_JSONL, "a", encoding="utf-8") as f:
            if _lead:
                f.write(_lead)
            for r in records:
                f.write(json.dumps(r, default=_json_default) + "\n")
            f.flush()
            os.fsync(f.fileno())
        # One-time directory fsync so the file's directory entry itself survives power loss
        # (the file-level fsync above does not sync the parent dir). Best-effort: on platforms
        # where a directory fd cannot be fsync'd (e.g. macOS EINVAL) this is a no-op — the OCI
        # Linux host, where it runs live, honors it. Never fail the write on this.
        if not _dir_fsynced:
            try:
                dfd = os.open(str(_JSONL.parent), os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except Exception:
                pass
            _dir_fsynced = True
        return True
    except Exception as e:
        _alert_write_fail(f"{len(records)} record(s)", e)
        return False


def _base(event: str, trade_id: str, symbol: str) -> dict:
    """Every record carries these fields (data-integrity seat): PT timestamp, event type, schema
    version, the join key (trade_id), and symbol."""
    return {
        "ts": datetime.now(PT).isoformat(),
        "event": event,
        "schema_v": SCHEMA_V,
        "trade_id": trade_id,
        "symbol": symbol,
    }


def _guard(fn: Callable) -> Callable:
    """NEVER-RAISES contract enforcement (cold-2nd #1/#2, 2026-09-02): the public logging functions
    build their record — float()/int()/round()/dict access — BEFORE handing it to _durable_append,
    so a malformed field would otherwise raise straight into the live trading runner. This wraps each
    public function so ANY exception is caught, logged loudly (never silent), and returned as False —
    a log failure can never crash or stall the caller (whose position is already placed + protected)."""
    @functools.wraps(fn)
    def _w(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.error("day_tier_logger.%s raised (record dropped, NOT re-raised): %s",
                         getattr(fn, "__name__", "?"), e)
            return False
    return _w


# ── Public logging API (called by the day-tier order module / runner) ────────────────────────

@_guard
def log_decision(decision_id: str, symbol: str, decision: dict | None = None,
                 trigger: dict | None = None, size: dict | None = None,
                 trade_id: str = "") -> bool:
    """Log a signal-tick decision-stack — including WAIT/no-entry decisions (why-no-entry is
    part of decision-explainability). `decision`/`trigger`/`size` are the pipeline dicts, written
    verbatim. trade_id is empty until an order is actually placed (a WAIT has no order); on an
    ENTER, pass the minted coid so the decision joins its entry_fill via decision_id."""
    rec = _base("decision", trade_id, symbol)
    rec["decision_id"] = decision_id
    rec["decision"] = decision or {}
    rec["trigger"] = trigger or {}
    rec["size"] = size or {}
    return _durable_append([rec])


@_guard
def log_entry_fill(trade_id: str, symbol: str, *, order_id: str, decision_id: str, side: str,
                   requested_limit: float, fill_price: float, fill_qty: float,
                   market_price_at_fill: float, equity_at_entry: float,
                   budget: float, notional: float) -> bool:
    """(1) Price snapshot at ENTRY execution — the fill price AND the market price at that instant
    (Rafael's amended spec). order_id + decision_id persisted for the offline coid<->order_id and
    decision joins."""
    rec = _base("entry_fill", trade_id, symbol)
    rec.update(
        order_id=order_id, decision_id=decision_id, side=side,
        requested_limit=round(float(requested_limit), 4),
        fill_price=round(float(fill_price), 4), fill_qty=float(fill_qty),
        market_price_at_fill=round(float(market_price_at_fill), 4),
        equity_at_entry=round(float(equity_at_entry), 2),
        budget=round(float(budget), 2), notional=round(float(notional), 2),
    )
    return _durable_append([rec])


@_guard
def log_stop_placed(trade_id: str, symbol: str, *, stop_order_id: str, stop_price: float) -> bool:
    """Log that a protective stop is live on the position (for the naked-check + audit)."""
    rec = _base("stop_placed", trade_id, symbol)
    rec.update(stop_order_id=stop_order_id, stop_price=round(float(stop_price), 4))
    return _durable_append([rec])


@_guard
def log_price_samples(samples: list[dict]) -> bool:
    """(2) Price PATH while open — one price_sample per OPEN trade per 30-min tick, batched into a
    SINGLE fsync (data-integrity seat). Each item must carry: trade_id, symbol, seq, market_price,
    unrealized_pnl. seq is the caller-supplied monotonic index (last_seq+1 from open_trades_from_
    log) so a missing sample is detectable. Empty list is a no-op success.

    PER-SAMPLE GUARDED (cold-2nd #1, 2026-09-02): a malformed sample dict (missing key / non-numeric
    field) is DROPPED with a loud ERROR and the OTHER open trades' samples STILL WRITE — one bad
    sample can never nuke the tick or raise into the caller. That preserves the keystone's zero-gap
    contract (a gap in ONE trade beats a gap in ALL of them, and the drop is never silent)."""
    recs = []
    dropped = 0
    for s in samples:
        try:
            rec = _base("price_sample", s["trade_id"], s.get("symbol", ""))
            rec.update(
                seq=int(s["seq"]),
                market_price=round(float(s["market_price"]), 4),
                unrealized_pnl=round(float(s.get("unrealized_pnl", 0.0)), 2),
            )
            recs.append(rec)
        except Exception as e:
            dropped += 1
            logger.error("day_tier_logger.log_price_samples: dropping malformed sample %r: %s", s, e)
    ok = _durable_append(recs)
    # A dropped sample is a price-path gap for that ONE trade — surface it, never swallow it.
    return ok and dropped == 0


@_guard
def log_exit_fill(trade_id: str, symbol: str, *, order_id: str, exit_reason: str,
                  fill_price: float, fill_qty: float, market_price_at_exit: float,
                  realized_pnl: float) -> bool:
    """(3) Price snapshot at EXIT — the exit fill price, the market price at exit, and the realized
    P&L (Rafael's amended spec). Its presence is what closes a trade_id in open_trades_from_log."""
    rec = _base("exit_fill", trade_id, symbol)
    rec.update(
        order_id=order_id, exit_reason=exit_reason,
        fill_price=round(float(fill_price), 4), fill_qty=float(fill_qty),
        market_price_at_exit=round(float(market_price_at_exit), 4),
        realized_pnl=round(float(realized_pnl), 2),
    )
    return _durable_append([rec])


# ── Restart-safe read side ───────────────────────────────────────────────────────────────────

def read_events(trade_id: str | None = None) -> list[dict]:
    """Read all events (optionally filtered to one trade_id), TOLERATING a torn trailing line: a
    `kill -9` mid-write before fsync can leave one partial JSON line, which is SKIPPED (+warned),
    never aborts the read (data-integrity seat #6). Returns [] if the file does not exist yet."""
    out: list[dict] = []
    if not _JSONL.exists():
        return out
    try:
        with open(_JSONL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    logger.warning("day_tier_events: skipping malformed/torn JSON line")
                    continue
                if trade_id is None or ev.get("trade_id") == trade_id:
                    out.append(ev)
    except Exception as e:
        logger.error(f"day_tier_events read failed: {e}")
    return out


def open_trades_from_log() -> dict[str, dict]:
    """STATELESS open-set derivation (the restart-safety keystone): replay the durable log and
    return every trade_id that has an entry_fill and NO exit_fill — the set of currently-open
    day-tier trades — each with the fields the 30-min sampler needs, INCLUDING last_seq (so the
    next price_sample uses seq = last_seq + 1). Because the log is append+fsync-durable and coid-
    keyed, this reconstructs the open set with zero gaps across ANY restart, so the price PATH of a
    trade open at restart is never holed. The runner reconciles this against live broker positions
    (source of truth) to also catch fills that occurred during downtime."""
    opened: dict[str, dict] = {}
    exited: set[str] = set()
    max_seq: dict[str, int] = {}
    for ev in read_events():
        tid = ev.get("trade_id")
        if not tid:
            continue
        et = ev.get("event")
        if et == "entry_fill":
            opened[tid] = {
                "trade_id": tid,
                "symbol": ev.get("symbol"),
                "side": ev.get("side"),
                "entry_price": ev.get("fill_price"),
                "fill_qty": ev.get("fill_qty"),
                "entry_ts": ev.get("ts"),
                "order_id": ev.get("order_id"),
                "decision_id": ev.get("decision_id"),
            }
        elif et == "exit_fill":
            exited.add(tid)
        elif et == "price_sample":
            s = ev.get("seq")
            if isinstance(s, int):
                max_seq[tid] = max(max_seq.get(tid, -1), s)
    # COID-UNIQUENESS ASSUMPTION (cold-2nd #3, 2026-09-02): `exited` permanently closes a trade_id,
    # so a REUSED coid across a later entry would be dropped here. That is safe ONLY because the
    # trade_id = make_coid embeds epoch_ms + an 8-hex uniq (see module header), so it is unique per
    # order and never reused. If that ever changes, this replay must key on (coid, entry_ts) instead.
    return {
        tid: {**info, "last_seq": max_seq.get(tid, -1)}
        for tid, info in opened.items()
        if tid not in exited
    }
