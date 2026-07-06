# ruff: noqa: E501 — aligned dict/print columns intentionally exceed 88 for readability
"""
reporting/pnl_ledger.py — authoritative stateless full-history FIFO P&L ledger.

Single source of truth for REALIZED P&L. Recomputes from the COMPLETE, immutable
Alpaca fill log on every call — NO symbol+side matching heuristic, NO incremental
prior-lots state. This structurally eliminates the RC-4 phantom-fill class
(a months-old fill matched as today's close), which survived ~5 prior symptom
patches because they hardened the heuristic instead of removing the ambiguity.

Design (board 2/2 + Gro + GAI unanimous, 2026-07-06):
  - Fetch ALL fills from account inception (rate-safe paginate, 429 backoff).
  - Sort chronologically; run pure FIFO per symbol (opens -> closes, longs+shorts).
  - Each realized round-trip carries a DATE-STAMPED id  {exit_date}-{symbol}-{seq}
    so a trade can never again be confused across days.
  - Because it recomputes from the immutable fill log, re-running auto-HEALS all
    historical corruption (7/2 phantom PANW/TSLA included) and can never drift.
  - Reconciliation INVARIANT: realized_lifetime + unrealized ~= equity - deposits
    (tolerance $5). Breach -> CRITICAL + Slack + flag. Makes a future phantom
    impossible to hide.

Data tier: T1 (Alpaca Paper Trading REST). Read-only; no SDK instantiation.

Phase 2a status: DIAGNOSTIC-ONLY. `python3 -m reporting.pnl_ledger` prints the
recomputed per-day + lifetime P&L side-by-side with the current (corrupt) eod /
lifetime-cache values and runs the invariant. It writes ONLY a diagnostic file to
logs/; it does NOT yet become the authoritative source (that is Phase 2b, gated).
"""
from __future__ import annotations

import os
import ssl
import json
import time
import logging
import urllib.request
import urllib.error
from collections import defaultdict, deque
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Path anchoring — always relative to this file, never CWD ──────────────────
_ROOT = Path(__file__).resolve().parent.parent
_LOGS = _ROOT / "logs"

_PAPER_BASE = "https://paper-api.alpaca.markets"
_TIMEOUT = 30.0
_INVARIANT_TOLERANCE = 5.00  # USD — data-integrity board (Kim): >$5 = structural

try:
    import certifi as _certifi  # noqa: F401
    _SSL_CTX = ssl.create_default_context(cafile=_certifi.where())
except Exception:  # pragma: no cover - certifi optional
    _SSL_CTX = ssl.create_default_context()


# ── Alpaca REST (rate-safe) ───────────────────────────────────────────────────
def _headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", "").strip(),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "").strip(),
    }


def _get_json(url: str, tries: int = 8):
    """
    GET with exponential backoff. Retries BOTH 429 rate-limits AND transient
    network/SSL errors (URLError, SSLError, socket timeout, connection reset) —
    a mid-fetch SSL EOF must not crash a full-history recompute. Non-429 HTTP
    errors (4xx/5xx that aren't 429) still raise immediately.
    """
    import socket
    last_err: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=_headers())
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=_TIMEOUT) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, ssl.SSLError, socket.timeout,
                ConnectionError, TimeoutError) as e:
            # Transient network/SSL blip — back off and retry.
            last_err = e
            logger.warning("transient network error (retry %d/%d): %s", i + 1, tries, e)
            time.sleep(1.5 * (i + 1))
            continue
    raise RuntimeError(f"Alpaca GET failed after {tries} tries: {last_err}")


def fetch_all_fills() -> list[dict]:
    """
    All FILL activities from account inception, chronological ascending.
    Rate-safe pagination via after_id with inter-page sleep + 429 backoff.
    """
    fills: list[dict] = []
    after_id: str | None = None
    while True:
        url = f"{_PAPER_BASE}/v2/account/activities/FILL?page_size=100"
        if after_id:
            url += f"&after_id={after_id}"
        batch = _get_json(url)
        if not isinstance(batch, list) or not batch:
            break
        fills.extend(batch)
        if len(batch) < 100:
            break
        after_id = batch[-1].get("id")
        time.sleep(0.5)
    fills.sort(key=lambda f: (f.get("transaction_time", ""), f.get("id", "")))
    return fills


def fetch_account() -> dict:
    return _get_json(f"{_PAPER_BASE}/v2/account")


def fetch_positions() -> list[dict]:
    data = _get_json(f"{_PAPER_BASE}/v2/positions")
    return data if isinstance(data, list) else []


def fetch_net_deposits() -> float:
    """
    Sum non-trade cash movements (CSD/CSW/deposits/withdrawals/transfers) so the
    invariant can isolate trade P&L from funding. Returns net deposited USD.
    """
    total = 0.0
    for atype in ("CSD", "CSW"):  # cash deposit / withdrawal
        after_id = None
        while True:
            url = f"{_PAPER_BASE}/v2/account/activities/{atype}?page_size=100"
            if after_id:
                url += f"&after_id={after_id}"
            try:
                batch = _get_json(url)
            except Exception as e:  # non-fatal — funding activities may be absent
                logger.warning("fetch_net_deposits(%s) failed: %s", atype, e)
                break
            if not isinstance(batch, list) or not batch:
                break
            for a in batch:
                try:
                    total += float(a.get("net_amount") or a.get("amount") or 0.0)
                except (TypeError, ValueError):
                    pass
            if len(batch) < 100:
                break
            after_id = batch[-1].get("id")
            time.sleep(0.3)
    return round(total, 2)


# ── Core: stateless full-history FIFO ─────────────────────────────────────────
def compute_realized(fills: list[dict], qhm_symbols: set | None = None) -> dict:
    """
    Pure chronological FIFO over ALL fills. No symbol+side heuristic, no state.

    Returns:
      round_trips        list[dict]  each realized close, DATE-STAMPED id
      per_day            {date: realized_pnl}
      per_day_intraday   {date: realized_pnl excluding qhm symbols}
      per_day_qhm        {date: realized_pnl for qhm symbols}
      lifetime           float (total realized)
      unmatched_closes   list[dict]  closes with no prior open lot (pre-log /
                         data gap) — FLAGGED, contribute $0, never fabricated
    """
    qhm_symbols = qhm_symbols or set()
    lots: dict[str, deque] = defaultdict(deque)  # sym -> deque[(qty, price, side, t)]
    round_trips: list[dict] = []
    unmatched: list[dict] = []
    per_day: dict[str, float] = defaultdict(float)
    per_day_intraday: dict[str, float] = defaultdict(float)
    per_day_qhm: dict[str, float] = defaultdict(float)
    seq_counter: dict[tuple, int] = defaultdict(int)

    def _close(sym, want_side, qty, price, t, day):
        """Match `qty` shares against open lots of `want_side`; realize P&L."""
        nonlocal round_trips
        dq = lots[sym]
        rem = qty
        realized_here = 0.0
        while rem > 0 and dq and dq[0][2] == want_side:
            lqty, lprice, lside, lt = dq[0]
            take = min(lqty, rem)
            if want_side == "long":
                pnl = (price - lprice) * take           # long: exit - entry
            else:
                pnl = (lprice - price) * take           # short: entry - exit
            realized_here += pnl
            seq_counter[(day, sym)] += 1
            round_trips.append({
                "id": f"{day}-{sym}-{seq_counter[(day, sym)]:03d}",
                "exit_date": day,
                "symbol": sym,
                "direction": want_side,
                "qty": take,
                "entry_price": round(lprice, 4),
                "exit_price": round(price, 4),
                "pnl": round(pnl, 2),
                "entry_time": lt,
                "exit_time": t,
                "is_qhm": sym in qhm_symbols,
            })
            if lqty == take:
                dq.popleft()
            else:
                dq[0] = (lqty - take, lprice, lside, lt)
            rem -= take
        if rem > 0:
            # No open lot of the needed side -> pre-inception lot or data gap.
            unmatched.append({
                "date": day, "symbol": sym, "close_side": want_side,
                "qty": rem, "price": round(price, 4), "time": t,
            })
        if realized_here:
            per_day[day] += realized_here
            if sym in qhm_symbols:
                per_day_qhm[day] += realized_here
            else:
                per_day_intraday[day] += realized_here
        return rem

    for f in fills:
        sym = f.get("symbol", "")
        side = f.get("side", "")
        try:
            qty = int(float(f.get("qty", 0)))
            price = float(f.get("price", 0.0))
        except (TypeError, ValueError):
            continue
        t = f.get("transaction_time", "")
        day = t[:10]
        if not sym or qty <= 0 or not day:
            continue
        dq = lots[sym]
        net = sum(q * (1 if sd == "long" else -1) for q, _p, sd, _t in dq)

        if side in ("buy", "buy_to_cover"):
            if net < 0:                       # cover short first
                leftover = _close(sym, "short", qty, price, t, day)
                if leftover > 0:
                    dq.append((leftover, price, "long", t))
            else:
                dq.append((qty, price, "long", t))
        elif side in ("sell", "sell_short"):
            if net > 0:                       # close long first
                leftover = _close(sym, "long", qty, price, t, day)
                if leftover > 0:
                    dq.append((leftover, price, "short", t))
            else:
                dq.append((qty, price, "short", t))

    open_lots = {s: [list(x) for x in dq] for s, dq in lots.items() if dq}
    return {
        "round_trips": round_trips,
        "per_day": {k: round(v, 2) for k, v in sorted(per_day.items())},
        "per_day_intraday": {k: round(v, 2) for k, v in sorted(per_day_intraday.items())},
        "per_day_qhm": {k: round(v, 2) for k, v in sorted(per_day_qhm.items())},
        "lifetime": round(sum(per_day.values()), 2),
        "lifetime_intraday": round(sum(per_day_intraday.values()), 2),
        "lifetime_qhm": round(sum(per_day_qhm.values()), 2),
        "unmatched_closes": unmatched,
        "open_lots": open_lots,
    }


def compute_unrealized(positions: list[dict]) -> float:
    total = 0.0
    for p in positions:
        try:
            total += float(p.get("unrealized_pl", 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
    return round(total, 2)


def check_invariant(lifetime_realized: float, unrealized: float,
                    equity: float, net_deposits: float,
                    tolerance: float = _INVARIANT_TOLERANCE) -> dict:
    """
    Master reconciliation: realized_lifetime + unrealized ~= equity - net_deposits.
    If deposits are unavailable, caller passes the assumed initial capital.
    """
    expected_total_pnl = round(equity - net_deposits, 2)
    computed_total_pnl = round(lifetime_realized + unrealized, 2)
    drift = round(abs(computed_total_pnl - expected_total_pnl), 2)
    return {
        "ok": drift <= tolerance,
        "drift": drift,
        "tolerance": tolerance,
        "computed_total_pnl": computed_total_pnl,
        "expected_total_pnl": expected_total_pnl,
        "lifetime_realized": lifetime_realized,
        "unrealized": unrealized,
        "equity": equity,
        "net_deposits": net_deposits,
    }


def _get_qhm_symbols() -> set:
    try:
        from execution.quarterly_hold_manager import get_quarterly_hold_symbols
        return set(get_quarterly_hold_symbols() or [])
    except Exception as e:
        logger.warning("QHM symbol lookup failed (treating none as QHM): %s", e)
        return set()


def build_ledger(assumed_initial_capital: float = 2500.0) -> dict:
    """Full authoritative ledger from live Alpaca data. Read-only."""
    fills = fetch_all_fills()
    acct = fetch_account()
    positions = fetch_positions()
    equity = float(acct.get("equity", 0.0) or 0.0)
    net_deposits = fetch_net_deposits() or assumed_initial_capital

    realized = compute_realized(fills, qhm_symbols=_get_qhm_symbols())
    unrealized = compute_unrealized(positions)
    inv = check_invariant(realized["lifetime"], unrealized, equity, net_deposits)

    return {
        "fills_count": len(fills),
        "earliest_fill": fills[0].get("transaction_time") if fills else None,
        "equity": equity,
        "net_deposits": net_deposits,
        "unrealized": unrealized,
        "invariant": inv,
        **realized,
    }


# ── Phase 2a diagnostic — compares vs current, writes NOTHING authoritative ────
def _load_current_eod_pnl() -> dict:
    out = {}
    for p in sorted(_LOGS.glob("eod_*.json")):
        try:
            d = json.loads(p.read_text())
            out[p.name[4:14]] = d.get("pnl_today")
        except Exception:
            continue
    return out


def diagnostic() -> dict:
    ledger = build_ledger()
    current = _load_current_eod_pnl()
    recomputed = ledger["per_day"]

    rows = []
    for day in sorted(set(current) | set(recomputed)):
        cur = current.get(day)
        rec = recomputed.get(day, 0.0)
        diff = None
        if cur is not None:
            try:
                diff = round(rec - float(cur), 2)
            except (TypeError, ValueError):
                diff = None
        rows.append({"date": day, "current": cur, "recomputed": rec, "diff": diff})

    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "fills_count": ledger["fills_count"],
        "earliest_fill": ledger["earliest_fill"],
        "lifetime_realized_recomputed": ledger["lifetime"],
        "lifetime_intraday": ledger["lifetime_intraday"],
        "lifetime_qhm": ledger["lifetime_qhm"],
        "unrealized": ledger["unrealized"],
        "invariant": ledger["invariant"],
        "unmatched_closes": ledger["unmatched_closes"],
        "per_day_compare": rows,
    }
    out_path = _LOGS / "pnl_ledger_diagnostic.json"
    try:
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=2))
        tmp.replace(out_path)
    except Exception as e:
        logger.warning("diagnostic write failed: %s", e)
    return report


def heal_history(dry_run: bool = True) -> dict:
    """
    Phase 2b heal: recompute ALL realized P&L from the stateless FIFO ledger and
    write the TRUE per-day values back into every eod_*.json, plus the lifetime
    cache. FAIL-CLOSED: if the reconciliation invariant does not hold (recomputed
    realized+unrealized != equity-deposits within $5), NOTHING is written — a bad
    recompute can never corrupt the pages. Idempotent; safe to re-run.

    dry_run=True (default): compute + return the plan, write nothing.
    dry_run=False: rewrite eod_*.json pnl_today (+ intraday/qhm split, clear
                   pnl_unreconciled, stamp _healed_by/_healed_at) and lifetime cache.
    """
    ledger = build_ledger()
    inv = ledger["invariant"]
    result: dict = {
        "invariant": inv,
        "lifetime_realized": ledger["lifetime"],
        "lifetime_intraday": ledger["lifetime_intraday"],
        "lifetime_qhm": ledger["lifetime_qhm"],
        "dry_run": dry_run,
        "eod_updated": [],
        "eod_missing_for_days": [],
        "lifetime_cache_updated": False,
        "aborted": False,
    }
    if not inv["ok"]:
        # FAIL-CLOSED: never write when the ledger does not reconcile to equity.
        result["aborted"] = True
        result["abort_reason"] = (
            f"invariant drift ${inv['drift']:.2f} > ${inv['tolerance']:.2f} — "
            f"recomputed ${inv['computed_total_pnl']:.2f} vs equity-deposits "
            f"${inv['expected_total_pnl']:.2f}. Refusing to write."
        )
        logger.critical("heal_history ABORT (fail-closed): %s", result["abort_reason"])
        try:
            from alerts import send_slack
            send_slack(f":rotating_light: P&L heal ABORTED — {result['abort_reason']}")
        except Exception:
            pass
        return result

    per_day = ledger["per_day"]
    per_day_intraday = ledger["per_day_intraday"]
    per_day_qhm = ledger["per_day_qhm"]
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")

    for day, true_pnl in per_day.items():
        eod_path = _LOGS / f"eod_{day}.json"
        if not eod_path.exists():
            result["eod_missing_for_days"].append(day)
            continue
        try:
            eod = json.loads(eod_path.read_text())
        except Exception as e:
            logger.warning("heal: cannot read %s: %s", eod_path.name, e)
            continue
        old = eod.get("pnl_today")
        # INCLUDE-SEPARATE (data-integrity board): pnl_today stays INTRADAY (its
        # historical meaning — weekly/monthly sum this field), QHM realized is a
        # SEPARATE line, and the reconciles-to-equity TOTAL is kept for reference.
        # This avoids any double-count and preserves downstream semantics.
        new_intraday = round(per_day_intraday.get(day, 0.0), 2)
        eod["pnl_today"] = new_intraday
        eod["pnl_today_qhm"] = round(per_day_qhm.get(day, 0.0), 2)
        eod["pnl_today_total"] = round(true_pnl, 2)  # intraday + qhm
        eod["pnl_unreconciled"] = False
        eod["pnl_unreconciled_reason"] = None
        eod["_healed_by"] = "pnl_ledger.heal_history"
        eod["_healed_at"] = now_iso
        result["eod_updated"].append({"date": day, "old": old, "new": new_intraday})
        if not dry_run:
            tmp = eod_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(eod, indent=2))
            tmp.replace(eod_path)

    # Lifetime cache — realized lifetime from the ledger (authoritative).
    if not dry_run:
        try:
            cache_path = _LOGS / "lifetime_pnl_cache.json"
            cache = {}
            if cache_path.exists():
                try:
                    cache = json.loads(cache_path.read_text())
                except Exception:
                    cache = {}
            cache["total_pnl"] = ledger["lifetime"]
            cache["total_pnl_intraday"] = ledger["lifetime_intraday"]
            cache["total_pnl_qhm"] = ledger["lifetime_qhm"]
            cache["ts"] = now_iso
            cache["type"] = "ledger_fifo_authoritative"
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, indent=2))
            tmp.replace(cache_path)
            result["lifetime_cache_updated"] = True
        except Exception as e:
            logger.warning("heal: lifetime cache write failed: %s", e)

    return result


def _acquire_singleton_lock():
    """
    Exclusive non-blocking lock so only ONE ledger run executes at a time.
    Any duplicate launch (cron overlap, manual re-run, retry storm) exits
    immediately instead of competing for the Alpaca rate limit. Returns the
    open file handle (kept alive to hold the lock) or None if already held.
    """
    import fcntl
    lock_path = _LOGS / ".pnl_ledger.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _lock = _acquire_singleton_lock()
    if _lock is None:
        print("another pnl_ledger instance is already running — exiting (singleton lock).")
        raise SystemExit(0)
    # Load .env for standalone runs
    _envp = _ROOT / ".env"
    if _envp.exists():
        for _line in _envp.read_text().splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

    import sys as _sys
    _mode = _sys.argv[1] if len(_sys.argv) > 1 else "--diagnostic"

    if _mode in ("--heal", "--heal-apply"):
        _apply = _mode == "--heal-apply"
        hr = heal_history(dry_run=not _apply)
        i = hr["invariant"]
        print("=" * 68)
        print(f"P&L HEAL — {'APPLY (writing)' if _apply else 'DRY-RUN (no writes)'}")
        print("=" * 68)
        print(f"INVARIANT ok={i['ok']} drift=${i['drift']:.2f} "
              f"(computed ${i['computed_total_pnl']:+.2f} vs equity-deposits ${i['expected_total_pnl']:+.2f})")
        if hr["aborted"]:
            print(f"ABORTED (fail-closed): {hr.get('abort_reason')}")
            raise SystemExit(1)
        print(f"lifetime realized: ${hr['lifetime_realized']:+,.2f} "
              f"(intraday ${hr['lifetime_intraday']:+,.2f} / qhm ${hr['lifetime_qhm']:+,.2f})")
        print(f"eod days to heal: {len(hr['eod_updated'])}  "
              f"(missing eod file for {len(hr['eod_missing_for_days'])} days)")
        for u in hr["eod_updated"]:
            if u["old"] != u["new"]:
                print(f"  {u['date']}: {u['old']}  ->  {u['new']:+.2f}")
        print(f"lifetime_cache_updated={hr['lifetime_cache_updated']}")
        raise SystemExit(0)

    rep = diagnostic()
    inv = rep["invariant"]
    print("=" * 68)
    print("STATELESS FIFO LEDGER — DIAGNOSTIC (writes nothing authoritative)")
    print("=" * 68)
    print(f"fills: {rep['fills_count']}  earliest: {rep['earliest_fill']}")
    print(f"lifetime realized (recomputed): ${rep['lifetime_realized_recomputed']:+,.2f}")
    print(f"  intraday: ${rep['lifetime_intraday']:+,.2f}   qhm: ${rep['lifetime_qhm']:+,.2f}")
    print(f"unrealized (open positions):    ${rep['unrealized']:+,.2f}")
    print("-" * 68)
    print(f"INVARIANT  realized+unrealized = ${inv['computed_total_pnl']:+,.2f}  "
          f"vs  equity-deposits = ${inv['expected_total_pnl']:+,.2f}")
    print(f"           drift ${inv['drift']:.2f}  (tol ${inv['tolerance']:.2f})  "
          f"=> {'PASS ✅' if inv['ok'] else 'FAIL ❌'}")
    if rep["unmatched_closes"]:
        print(f"unmatched closes (pre-log/gap, $0 fabricated): {len(rep['unmatched_closes'])}")
        for u in rep["unmatched_closes"][:10]:
            print(f"    {u['date']} {u['symbol']} {u['close_side']} x{u['qty']} @ ${u['price']}")
    print("-" * 68)
    print("PER-DAY: current(corrupt) -> recomputed(true)  [only diffs shown]")
    for r in rep["per_day_compare"]:
        if r["diff"] not in (0.0, None) or (r["current"] in (0, 0.0) and r["recomputed"]):
            print(f"  {r['date']}: {r['current']}  ->  {r['recomputed']:+.2f}"
                  + (f"   (Δ {r['diff']:+.2f})" if r["diff"] is not None else "   (no current)"))
