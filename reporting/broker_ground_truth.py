# ruff: noqa: E501 — long docstring/comment lines document the classification rules
"""
reporting/broker_ground_truth.py — per-position BROKER stop coverage for one RTH session.

Why this exists (audit-alert false alarms, Rafael 2026-09-23; board 2 seats + Gro + GAI aligned;
design record logs/design_records/audit_broker_ground_truth_2026-09-24.md): the LLM audits
inferred "position naked" from LOG TEXT (e.g. the pre-close sweep's "placed MISSING buy stop" or
"broker-held 0") and posted CATASTROPHIC for positions protected exactly as designed. This module
computes, in CODE and from the broker's own order + fill history, how many RTH minutes each
position spent without a resting broker stop for its full quantity, and why:

  COVERED                    broker stop(s) covered the full qty (<= _TOLERANCE_MIN uncovered)
  SOFTWARE-ONLY-BY-DESIGN    uncovered minutes on a position owned ONLY by the core intraday
                             strategy (client_order_id "IN-") while the bot's cycle loop was
                             running — core intraday entries are protected by a software stop
                             until the pre-close sweep places a broker DAY stop
  SOFTWARE-ONLY+CYCLE-GAP    uncovered minutes on a core position during a cycle gap longer than
                             _CYCLE_GAP_MIN — the software stop was NOT being evaluated
  BROKER-STOP-LAPSED         uncovered minutes on a core holding AFTER the design says a broker
                             stop should rest (it had one and lost it, or a carried holding past
                             the opening window) — software stop still running, broker stop gone
  NAKED                      uncovered minutes on a position not solely core-owned (day tier,
                             quarterly holds, unknown owner) — nothing protects it in software
  UNKNOWN                    the data needed to decide could not be read — NEVER reported as OK

Masked-loss invariant: a symbol is cleared (COVERED / BY-DESIGN) only on POSITIVE broker
evidence; any read failure, truncated page, or unreadable cycle log yields UNKNOWN. The audits use
this block as prompt context and to ADD deterministic alarms — never to remove or downgrade an LLM
finding.

Data tier: T1 (Alpaca Paper Trading REST, read-only) via reporting.pnl_ledger._get_json — no SDK
client, no order placement. Called only from post-session audit scripts, never the trade loop.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")   # every user-facing time is PT (CLAUDE.md §8); ET is internal
UTC = ZoneInfo("UTC")

_ROOT = Path(__file__).resolve().parent.parent
BOT_LOG = _ROOT / "logs" / "mtf_bot.log"

_TOLERANCE_MIN = 2.0        # uncovered minutes ignored (fill→stop submit latency, clock skew)
# Cycle-gap threshold DERIVED from production data (mtf_bot.log "[CYCLE] duration=" lines, 2,636
# RTH gaps 2026-07-19..2026-09-23): median 6.8 min, p99 10.6, p99.9 14.2, max 43.5. > 15 min is
# beyond p99.9 → the loop genuinely was not evaluating software stops.
_CYCLE_GAP_MIN = 15.0
# Resting GTC stops can be MONTHS old (2026-09-14: GE's quarterly-hold stop was created 07-27 and
# filled 09-15 — a 45-day lookback missed it and falsely classed GE NAKED). So the FULL order
# history up to the close is read; more than _MAX_ORDER_PAGES x 500 orders → UNKNOWN, not a guess.
_MAX_ORDER_PAGES = 40
_MAX_FILL_PAGES = 50
_STOP_TYPES = ("stop", "stop_limit", "trailing_stop")
_TERMINAL_FIELDS = ("filled_at", "canceled_at", "expired_at", "replaced_at", "failed_at")
_TERMINAL_STATUSES = ("filled", "canceled", "expired", "replaced", "done_for_day", "stopped",
                      "suspended", "calculated")
# The pre-close sweep places core DAY stops ~15:45-15:52 ET (observed 2026-09-02..09-23). A fresh
# core holding still without a broker stop after close − _SWEEP_DEADLINE_MIN is a LAPSE, not design.
_SWEEP_DEADLINE_MIN = 5.0
# entry_logic places a broker GTC stop AT ENTRY for core entries at/after 15:30 ET — a WALL-CLOCK
# rule (execution/entry_logic.py `entry_mins >= 15*60+30`), so the classifier uses 15:30 ET capped at
# the session close (an early-close day has no such window): no by-design software-only window.
_GTC_AT_ENTRY_ET = (15, 30)
# A lapse this long is act-now (critical, fails the card): 2026-09-18 UBER went 204 min without a
# broker stop after the bot logged "2 shares unprotected. Set manual stop in Alpaca".
_LAPSE_CRITICAL_MIN = 60.0

CLASS_COVERED = "COVERED"
CLASS_DESIGN = "SOFTWARE-ONLY-BY-DESIGN"
CLASS_GAP = "SOFTWARE-ONLY+CYCLE-GAP"
CLASS_NAKED = "NAKED"
CLASS_LAPSED = "BROKER-STOP-LAPSED"
CLASS_UNKNOWN = "UNKNOWN"
CLEARED_CLASSES = (CLASS_COVERED, CLASS_DESIGN)
ALARM_CLASSES = (CLASS_NAKED, CLASS_GAP, CLASS_LAPSED, CLASS_UNKNOWN)


def _ts(s):
    from reporting.pnl_ledger import _iso_to_dt
    return _iso_to_dt(s) if isinstance(s, str) else None


def _tier(coid) -> str:
    c = coid or ""
    if c.startswith("IN-"):
        return "core"
    if c.startswith("DT-"):
        return "day"
    if c.startswith("QH-"):
        return "qhm"
    if c.startswith("F6-"):
        return "f6"      # forever-hold floor: stop_protection excludes it — no stop BY DESIGN
    return "other"


def _signed(fill: dict) -> float:
    q = float(fill.get("qty") or 0.0)
    return q if fill.get("side") == "buy" else -q


# ── Pure core ─────────────────────────────────────────────────────────────────

def cycle_gaps(cycle_ts: list, open_dt: datetime, close_dt: datetime) -> list:
    """Intervals inside [open, close) with no [CYCLE] line for more than _CYCLE_GAP_MIN,
    including open→first cycle and last cycle→close. No cycle at all → the whole session."""
    lo = open_dt - timedelta(minutes=_CYCLE_GAP_MIN)
    pts = sorted(t for t in cycle_ts if lo <= t <= close_dt)
    gaps = []
    prev = open_dt
    for t in pts:
        if t <= prev:
            continue
        if (t - prev).total_seconds() / 60 > _CYCLE_GAP_MIN:
            gaps.append((prev, t))
        prev = t
    if (close_dt - prev).total_seconds() / 60 > _CYCLE_GAP_MIN:
        gaps.append((prev, close_dt))
    return gaps


def classify(open_dt: datetime, close_dt: datetime, orders: list, fills: list,
             positions_now: dict, cycle_ts: list | None,
             window_end: datetime | None = None) -> dict:
    """Pure classification. `orders` = order dicts (OCO/bracket legs nested under "legs"), any
    status; `fills` = EVERY FILL activity from `open_dt` to now; `positions_now` = {symbol:
    signed qty}; `cycle_ts` = aware datetimes of [CYCLE] lines, or None if the log was unreadable.
    `window_end` (default close_dt) is where evaluation stops — earlier than close_dt for a
    mid-session caller; the 15:30 / 15:55 design rules always key off the real session close.
    Raises ValueError on an unparseable fill time (the caller turns it into UNKNOWN)."""
    end = window_end or close_dt
    flat: list = []
    parent_coid: dict = {}
    for o in orders:
        flat.append(o)
        for leg in o.get("legs") or []:
            flat.append(leg)
            if leg.get("id"):
                parent_coid[leg["id"]] = o.get("client_order_id")

    def coid_of(o_id, o=None):
        if o_id in parent_coid:
            return parent_coid[o_id]
        if o is not None:
            return o.get("client_order_id")
        for x in flat:
            if x.get("id") == o_id:
                return x.get("client_order_id")
        return None

    # Session-open quantity = current book with every fill since the open backed out.
    start = dict(positions_now)
    session_fills = []
    for f in fills:
        t = _ts(f.get("transaction_time"))
        if t is None:
            raise ValueError(f"unparseable fill time {f.get('transaction_time')!r}")
        start[f["symbol"]] = start.get(f["symbol"], 0.0) - _signed(f)
        if t < end:
            session_fills.append((t, f))
    session_fills.sort(key=lambda x: x[0])

    gaps = cycle_gaps(cycle_ts, open_dt, end) if cycle_ts is not None else None
    symbols = ({s for s, q in start.items() if abs(q) > 1e-9}
               | {f["symbol"] for _, f in session_fills})
    out: dict = {}
    for sym in sorted(symbols):
        carried = abs(start.get(sym, 0.0)) > 1e-9
        stops_any = []
        for o in flat:
            if (o.get("symbol") != sym or o.get("type") not in _STOP_TYPES
                    or o.get("status") == "rejected"):
                continue
            a = _ts(o.get("created_at"))   # OCO/bracket legs rest at the broker from creation
            if a is None:
                continue
            b = None
            for k in _TERMINAL_FIELDS:            # first PARSEABLE end time wins
                if o.get(k):
                    b = _ts(o[k])
                    if b is not None:
                        break
            if b is None and o.get("status") in _TERMINAL_STATUSES:
                continue                    # ended, but when is unreadable → grant no coverage
            stops_any.append((a, b or end, o.get("side"), float(o.get("qty") or 0.0),
                              _tier(coid_of(o.get("id"), o))))
        stops = [x for x in stops_any if x[0] < end and x[1] > open_dt]

        # Segments of constant quantity, each with the OWNER set of the holding it belongs to.
        # A carried position's owner = the tier(s) of the same-side stop(s) ALIVE OVERNIGHT (placed
        # at/before the open and still resting within the 12h before it — the overnight GTC, usually
        # cancelled just before the open); if none were alive overnight, the union of every tier
        # seen on that side in the prior 24h, else of the first stop placed in the session; none →
        # owner unknown ("other"). A mixed set is never core-only, so never "by design" (cold-2nd
        # r6: a stale expired core stop must not relabel a quarterly-hold position as core). A
        # position that goes flat starts a new holding owned by the tier of the fill that opens it.
        q = start.get(sym, 0.0)
        owners: set = set()
        if carried:
            want = "sell" if q > 0 else "buy"
            lo = open_dt - timedelta(hours=24)
            cands = [(a, tr) for a, b, side, _q, tr in stops_any
                     if side == want and a < end and b > lo]
            before = [c for c in cands if c[0] <= open_dt]
            overnight = {tr for a, b, side, _q, tr in stops_any
                         if side == want and a <= open_dt and b > open_dt - timedelta(hours=12)}
            if overnight:            # the stop(s) that actually protected it overnight
                owners = set(overnight)
            elif before:             # none alive overnight → every tier seen before the open
                owners = {c[1] for c in before}
            elif cands:     # none before the open → the first one placed during the session
                owners = {min(cands, key=lambda c: c[0])[1]}
            else:
                owners = {"other"}
        t0 = open_dt
        segs = []
        all_owners: set = set(owners)
        hid = 0                                   # holding id; 0 = the carried holding (if any)
        hold_start: dict = {0: open_dt}
        for t, f in (x for x in session_fills if x[1]["symbol"] == sym):
            if abs(q) > 1e-9 and t > t0:
                segs.append((t0, t, q, frozenset(owners), hid))
            new_q = q + _signed(f)
            tier = _tier(coid_of(f.get("order_id")))
            if abs(q) <= 1e-9 or q * new_q < 0:          # fresh holding (from flat, or flipped)
                owners = {tier}
                hid += 1
                hold_start[hid] = t
            elif abs(new_q) > abs(q) + 1e-9:              # added to an existing holding
                owners.add(tier)
            all_owners |= owners
            q, t0 = new_q, t
        if abs(q) > 1e-9 and end > t0:
            segs.append((t0, end, q, frozenset(owners), hid))
        if not segs:
            continue

        # Core design (execution/entry_logic.py + strategy/run_cycle.py): a FRESH core intraday
        # holding is software-protected until its first broker stop; a CARRIED core holding gets
        # a broker DAY stop on the first cycle after the open (allowed: open + _CYCLE_GAP_MIN).
        # Any uncovered minute AFTER that — a holding that HAD broker protection and lost it
        # (e.g. 2026-09-18 UBER: breakeven-push stop resubmit failed, 3.5h without a broker stop)
        # — is a LAPSE, not design.
        open_window_end = open_dt + timedelta(minutes=_CYCLE_GAP_MIN)
        sweep_deadline = close_dt - timedelta(minutes=_SWEEP_DEADLINE_MIN)
        gtc_at_entry_from = min(close_dt, open_dt.replace(hour=_GTC_AT_ENTRY_ET[0],
                                                          minute=_GTC_AT_ENTRY_ET[1]))
        covered_once: set = set()
        exposed = unc_design = unc_gap = unc_other = unc_lapsed = 0.0
        uncovered_at_close = False
        stretches: list = []
        for a, b, qq, seg_owners, seg_hid in segs:
            core_only = seg_owners == {"core"}
            need = "sell" if qq > 0 else "buy"
            # Exact sweep: coverage is constant between consecutive breakpoints (stop starts/ends,
            # cycle-gap edges, the opening-window end), so evaluate each sub-interval once.
            pts = {a, b}
            for sa, sb, _side, _q, _tr in stops:
                pts.update(x for x in (sa, sb) if a < x < b)
            for ga, gb in (gaps or []):
                pts.update(x for x in (ga, gb) if a < x < b)
            for x in (open_window_end, sweep_deadline):
                if a < x < b:
                    pts.add(x)
            bps = sorted(pts)
            for m, n in zip(bps, bps[1:], strict=False):  # consecutive pairs
                dur = (n - m).total_seconds() / 60
                exposed += dur
                held = sum(sq for sa, sb, side, sq, _tr in stops if side == need and sa <= m < sb)
                if held + 1e-9 >= abs(qq):
                    covered_once.add(seg_hid)
                    continue
                if n >= end:
                    uncovered_at_close = seg_owners != {"f6"}   # F6: stop-free by design
                lapsed = (seg_hid in covered_once
                          or (seg_hid == 0 and carried and m >= open_window_end)
                          or m >= sweep_deadline
                          or (seg_hid != 0 and hold_start.get(seg_hid, open_dt) >= gtc_at_entry_from))
                if seg_owners == {"f6"}:
                    unc_design += dur          # F6 floor shares carry no stop by design
                elif not core_only:
                    unc_other += dur
                elif gaps is None or any(ga <= m < gb for ga, gb in gaps):
                    unc_gap += dur
                elif lapsed:
                    unc_lapsed += dur
                else:
                    unc_design += dur
                if stretches and stretches[-1][1] >= m:
                    stretches[-1][1] = n
                else:
                    stretches.append([m, n])
        rejected = sum(1 for o in flat if o.get("symbol") == sym and o.get("status") == "rejected"
                       and o.get("type") in _STOP_TYPES)
        if unc_other > _TOLERANCE_MIN:
            cls = CLASS_NAKED
        elif unc_gap > _TOLERANCE_MIN:
            cls = CLASS_UNKNOWN if gaps is None else CLASS_GAP
        elif unc_lapsed > _TOLERANCE_MIN:
            cls = CLASS_LAPSED
        elif unc_design > _TOLERANCE_MIN:
            cls = CLASS_DESIGN
        else:
            cls = CLASS_COVERED
        if uncovered_at_close and end >= close_dt and cls in CLEARED_CLASSES:
            # Held into the SESSION CLOSE with no broker stop — never "covered" or "by design",
            # however few minutes (masked-loss seat R3: an exit cancelled the stop at 15:58 and
            # never filled → carried out of the session unprotected).
            cls = CLASS_LAPSED
        out[sym] = {
            "class": cls,
            "owners": sorted(all_owners),
            "exposed_min": round(exposed),
            "uncovered_min": round(unc_design + unc_gap + unc_other + unc_lapsed),
            "lapsed_min": round(unc_lapsed),
            "uncovered_at_close": uncovered_at_close,
            "uncovered_windows_et": [f"{s.astimezone(ET):%H:%M}-{e.astimezone(ET):%H:%M}"
                                     for s, e in stretches],
            "uncovered_windows_pt": [f"{s.astimezone(PT):%H:%M}-{e.astimezone(PT):%H:%M}"
                                     for s, e in stretches],
            "rejected_stop_orders": rejected,
        }
    return {"positions": out,
            "cycle_log": "OK" if gaps is not None else "UNKNOWN",
            "cycle_gaps_pt": [f"{a.astimezone(PT):%H:%M}-{b.astimezone(PT):%H:%M}"
                              for a, b in (gaps or [])],
            "cycle_gaps_et": [f"{a.astimezone(ET):%H:%M}-{b.astimezone(ET):%H:%M}"
                              for a, b in (gaps or [])]}


# ── I/O layer ─────────────────────────────────────────────────────────────────

def read_cycle_times(log_path: Path | None = None, since: datetime | None = None):
    """Aware UTC datetimes of every '[CYCLE] duration=' line (main.py logs one per loop cycle).
    Log timestamps are UTC (OCI host clock). Returns None if the log is unreadable."""
    out = []
    try:
        with open(log_path or BOT_LOG, errors="replace") as fh:
            for line in fh:
                if "[CYCLE] duration=" not in line:
                    continue
                try:
                    t = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                except ValueError:
                    continue
                if since is None or t >= since:
                    out.append(t)
    except OSError as e:
        logger.warning("broker_ground_truth: bot log unreadable (%s) — cycle status UNKNOWN", e)
        return None
    return out


def _page_size(batch: list) -> int:
    """Orders Alpaca counted toward `limit` for a nested=true page: top-level orders PLUS their
    legs (verified live 2026-09-24: a limit=500 page returned 494 orders + 6 OCO legs). Using only
    len(batch) mistook a full page for the last one and silently truncated the history."""
    return len(batch) + sum(len(o.get("legs") or []) for o in batch if isinstance(o, dict))


def _fetch_orders(open_dt: datetime, close_dt: datetime) -> list:
    """Every order that could have rested during the session: the account's full order history
    up to the close (paged newest-first by an `until` cursor on submitted_at, overlap-and-dedup)
    + everything open now."""
    from reporting.pnl_ledger import _PAPER_BASE, _bump_iso_ms, _get_json
    until = close_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen: dict = {}
    for _ in range(_MAX_ORDER_PAGES):
        batch = _get_json(f"{_PAPER_BASE}/v2/orders?status=all&limit=500&direction=desc"
                          f"&nested=true&until={until}")
        if not isinstance(batch, list):
            raise RuntimeError(f"orders: non-list response {str(batch)[:120]!r}")
        new = [o for o in batch if isinstance(o, dict) and o.get("id") not in seen]
        for o in new:
            seen[o["id"]] = o
        # End of history = a page that brings NO new orders (the overlap page after the last real
        # one, or an empty page). Never inferred from a short page: how Alpaca fills a page when
        # legs count toward `limit` is not something this code should depend on (cold-2nd r9).
        if not new:
            if _page_size(batch) >= 500:
                raise RuntimeError("orders: full page with no new ids — history incomplete")
            break
        # Alpaca's `until` filters on SUBMITTED_at (verified live 2026-09-24: an overnight GTC stop
        # created 06:00:35 / submitted 08:00:45 was missing with until=06:00:38.503 and present with
        # until=08:00:45.200). A created_at cursor skipped such orders at a page boundary.
        _last = batch[-1] if isinstance(batch[-1], dict) else {}
        nxt = _bump_iso_ms(_last.get("submitted_at") or _last.get("created_at") or "")
        if not nxt:
            raise RuntimeError("orders: unparseable page boundary — history incomplete")
        until = nxt
    else:
        raise RuntimeError(f"orders: more than {_MAX_ORDER_PAGES} pages — history truncated")
    now_open = _get_json(f"{_PAPER_BASE}/v2/orders?status=open&limit=500&nested=true")
    if not isinstance(now_open, list) or _page_size(now_open) >= 500:
        raise RuntimeError("open orders: unreadable or truncated")
    for o in now_open:
        seen.setdefault(o["id"], o)
    return list(seen.values())


def _fetch_fills_since(open_dt: datetime) -> list:
    from reporting.pnl_ledger import _PAPER_BASE, _get_json
    after = open_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list = []
    token = None
    for _ in range(_MAX_FILL_PAGES):
        url = (f"{_PAPER_BASE}/v2/account/activities/FILL?after={after}"
               "&direction=asc&page_size=100")
        if token:
            url += f"&page_token={token}"
        batch = _get_json(url)
        if not isinstance(batch, list):
            raise RuntimeError(f"fills: non-list response {str(batch)[:120]!r}")
        out.extend(batch)
        if len(batch) < 100:
            return out
        token = batch[-1].get("id")
    raise RuntimeError(f"fills: more than {_MAX_FILL_PAGES} pages — truncated")


def _session_bounds(day: str):
    """(open_dt, close_dt) in ET from the Alpaca calendar, or None if `day` is not a session."""
    from reporting.pnl_ledger import _PAPER_BASE, _get_json
    cal = _get_json(f"{_PAPER_BASE}/v2/calendar?start={day}&end={day}")
    if not isinstance(cal, list):
        raise RuntimeError(f"calendar: non-list response {str(cal)[:120]!r}")
    for c in cal:
        if c.get("date") == day:
            d = datetime.strptime(day, "%Y-%m-%d")
            oh, om = (int(x) for x in c["open"].split(":"))
            ch, cm = (int(x) for x in c["close"].split(":"))
            return (d.replace(hour=oh, minute=om, tzinfo=ET),
                    d.replace(hour=ch, minute=cm, tzinfo=ET))
    return None


def collect(day: str, now: datetime | None = None) -> dict:
    """Ground truth for ET session `day` (YYYY-MM-DD), evaluated up to min(close, now) so a
    mid-session caller never counts the future. Never raises: any failure → status UNKNOWN with
    the reason (fails toward visibility, never toward 'OK')."""
    try:
        bounds = _session_bounds(day)
        if bounds is None:
            return {"status": "NO_SESSION", "session": day, "positions": {}}
        open_dt, close_dt = bounds
        now = now or datetime.now(ET)
        if now <= open_dt:
            return {"status": "NO_SESSION", "session": day, "positions": {},
                    "reason": "session not yet open"}
        end_dt = min(close_dt, now)
        from reporting.pnl_ledger import _PAPER_BASE, _get_json
        fills_before = _fetch_fills_since(open_dt)
        pos = _get_json(f"{_PAPER_BASE}/v2/positions")
        if not isinstance(pos, list):
            raise RuntimeError(f"positions: non-list response {str(pos)[:120]!r}")
        positions_now = {p["symbol"]: float(p["qty"]) for p in pos}
        # Fills are read BEFORE and AFTER the positions snapshot: a fill landing in between would
        # make the backed-out session quantity wrong (cold-2nd r5: could turn NAKED into COVERED).
        fills = _fetch_fills_since(open_dt)
        if len(fills) != len(fills_before):
            raise RuntimeError("fills changed during the snapshot — book not consistent, retry later")
        orders = _fetch_orders(open_dt, end_dt)
        cyc = read_cycle_times(since=open_dt - timedelta(hours=1))
        res = classify(open_dt, close_dt, orders, fills, positions_now, cyc, window_end=end_dt)
        res.update(status="OK", session=day, window_et=f"{open_dt:%H:%M}-{end_dt:%H:%M}",
                   window_pt=f"{open_dt.astimezone(PT):%H:%M}-{end_dt.astimezone(PT):%H:%M}")
        return res
    except Exception as e:   # the audit must still post; say UNKNOWN loudly instead
        logger.warning("broker_ground_truth: collect(%s) failed — UNKNOWN: %s", day, e)
        return {"status": "UNKNOWN", "session": day, "reason": str(e)[:300], "positions": {}}


def render(gt: dict) -> str:
    """Plain-text block for an LLM prompt / report file."""
    st = gt.get("status")
    if st == "NO_SESSION":
        return (f"BROKER GROUND TRUTH ({gt.get('session')}): no trading session in range — "
                "nothing to classify.")
    if st != "OK":
        return (f"BROKER GROUND TRUTH ({gt.get('session')}): UNKNOWN — broker data could not be "
                f"read ({gt.get('reason', 'n/a')}). Stop coverage is UNVERIFIED: report it as "
                "UNKNOWN and never assert 'protected'. A naked position may be reported ONLY when "
                "the bot's own log states it (e.g. 'unprotected', a stop resubmit 'FAILED') — "
                "quote that line and tag it broker-unverified.")
    lines = [f"BROKER GROUND TRUTH ({gt['session']}, RTH {gt.get('window_pt', '')} PT) — computed "
             f"by code from Alpaca order + fill history. Bot cycle log: {gt.get('cycle_log')}; "
             f"cycle gaps > {_CYCLE_GAP_MIN:.0f} min: "
             f"{', '.join(gt.get('cycle_gaps_pt') or []) or 'none'} (times PT)"]
    if not gt.get("positions"):
        lines.append("  (no positions held during the session)")
    for sym, p in gt["positions"].items():
        w = ", ".join(p.get("uncovered_windows_pt", [])[:4]) or "-"
        lines.append(f"  {sym:6} {p['class']:24} owners={'/'.join(p['owners']) or '?'} "
                     f"exposed={p['exposed_min']}m no-broker-stop={p['uncovered_min']}m [{w}] "
                     f"rejected_stop_orders={p['rejected_stop_orders']}")
    return "\n".join(lines)


def alarm_findings(gt: dict) -> list:
    """Deterministic card findings. Each: {"severity", "title", "detail"} (scripts/audit_slack
    schema). NAKED, SOFTWARE-ONLY+CYCLE-GAP, and a lapse of >= _LAPSE_CRITICAL_MIN = critical;
    BROKER-STOP-LAPSED = high, or low when
    it lapsed < _CYCLE_GAP_MIN min and was covered again by the window end; UNKNOWN = high; any
    position still uncovered at the window end = at least high; a bot-loop stall (a cycle gap)
    or an unreadable cycle log = one position-independent high finding; an unreadable ground
    truth = one high finding. These findings only ever ADD to an audit card."""
    if gt.get("status") == "NO_SESSION":
        return []
    if gt.get("status") != "OK":
        return [{"severity": "high",
                 "title": "Broker stop coverage UNKNOWN — broker data unreadable",
                 "detail": str(gt.get("reason", ""))[:200]}]
    out = []
    if gt.get("cycle_log") != "OK":
        out.append({"severity": "high",
                    "title": "Bot cycle log UNREADABLE (broker ground truth)",
                    "detail": ("could not read logs/mtf_bot.log [CYCLE] lines — whether the bot "
                               "loop (software stops) ran is UNKNOWN")})
    if gt.get("cycle_gaps_et"):
        out.append({"severity": "high",
                    "title": f"Bot loop stalled > {_CYCLE_GAP_MIN:.0f} min (broker ground truth)",
                    "detail": ("no [CYCLE] line during " + ", ".join(gt.get("cycle_gaps_pt", [])[:4])
                               + " PT — software stops were not evaluated")})
    for sym, p in (gt.get("positions") or {}).items():
        if p["class"] not in ALARM_CLASSES:
            continue
        if p["class"] in (CLASS_NAKED, CLASS_GAP) or (
                p["class"] == CLASS_LAPSED and p.get("lapsed_min", 0) >= _LAPSE_CRITICAL_MIN):
            sev = "critical"
        elif (p["class"] == CLASS_LAPSED and p.get("lapsed_min", 0) < _CYCLE_GAP_MIN
              and not p.get("uncovered_at_close")):
            sev = "low"       # short lapse, resolved (e.g. exit cancelled the stop, filled minutes later)
        else:
            sev = "high"
        still = " — STILL UNCOVERED at the window end" if p.get("uncovered_at_close") else ""
        lapsed = (f" (lapsed {p.get('lapsed_min', 0)}m beyond design)"
                  if p["class"] == CLASS_LAPSED else "")
        out.append({"severity": sev,
                    "title": f"{sym} {p['class']} (broker ground truth){still}",
                    "detail": (f"{sym}: no broker stop for {p['uncovered_min']}m of "
                               f"{p['exposed_min']}m held{lapsed} "
                               f"({', '.join(p.get('uncovered_windows_pt', [])[:3])} PT); owners "
                               f"{'/'.join(p['owners'])}")})
    return out


_CARD_ALARM_CAP = 8   # render_card uses ~2 blocks per finding; Slack caps a message at 50 blocks


def card_alarms(gt: dict) -> tuple:
    """Split alarm_findings for the Slack card: (critical/high findings for the card, capped at
    _CARD_ALARM_CAP with a '+N more' high line, low-severity summary text for the footer or "").
    Lows never enter the card's findings list — render_card collapses >2 lows into a bare count,
    which would hide the LLM's own low findings (cold-2nd r5)."""
    al = alarm_findings(gt)
    rank = {"critical": 0, "high": 1}
    loud = sorted((a for a in al if a["severity"] in rank), key=lambda a: rank[a["severity"]])
    lows = [a for a in al if a["severity"] not in rank]
    if len(loud) > _CARD_ALARM_CAP:
        extra = len(loud) - (_CARD_ALARM_CAP - 1)
        loud = loud[:_CARD_ALARM_CAP - 1] + [{
            "severity": "high", "title": f"+{extra} more broker stop alarm(s)",
            "detail": "see the BROKER GROUND TRUTH block in the full report"}]
    low_txt = "; ".join(a["detail"] for a in lows)
    return loud, low_txt


def checked_summary(gt: dict, limit: int = 12) -> str:
    """Compact 'SYM CLASS' list of the positions the check covered (card footer)."""
    pos = gt.get("positions") or {}
    short = {CLASS_COVERED: "covered", CLASS_DESIGN: "software-by-design", CLASS_LAPSED: "LAPSED",
             CLASS_GAP: "CYCLE-GAP", CLASS_NAKED: "NAKED", CLASS_UNKNOWN: "UNKNOWN"}
    items = [f"{s} {short.get(p['class'], p['class'])}" for s, p in pos.items()]
    more = f" +{len(items) - limit} more" if len(items) > limit else ""
    return " · ".join(items[:limit]) + more


# ── Prompt-compliance DETECTOR (detect-only; never changes a finding or verdict) ─
# Revision 5 removed every code path that downgrades LLM findings (four cold reviews showed prose
# parsing could hide a real catastrophic claim). The replacement control is the prompt rule "stop
# coverage is owned by code". This detector COUNTS violations of that rule so the reversal
# criterion is measured by code, not memory (observability seat, 2026-09-24). A wrong or missed
# match only mis-counts — it can never hide or lower an alarm.
_NAKED_WORDS = re.compile(r"naked|unprotected|no (?:broker |protective |live )?stop|missing (?:buy |sell )?stop|"
                          r"stop[^.\n]{0,40}\bmissing|without (?:a |any )?stop", re.I)


def naked_claims_on_cleared(report: str, gt: dict) -> list:
    """Symbols the LLM called naked in CATASTROPHIC ALERT (a line carrying naked wording and the
    symbol) although the ground truth classes them COVERED or BY-DESIGN. NEW BUGS is not counted:
    the prompt routes quoted bot self-reports of stop failures there on purpose."""
    if gt.get("status") != "OK":
        return []
    cleared = {s for s, p in (gt.get("positions") or {}).items() if p["class"] in CLEARED_CLASSES}
    hits: set = set()
    section = False
    for line in (report or "").splitlines():
        up = line.strip().lstrip("#* ").upper()
        if up.startswith("CATASTROPHIC ALERT"):
            section = True
            continue
        if line.lstrip().startswith("###"):
            section = False
            continue
        if section and _NAKED_WORDS.search(line):
            hits |= {s for s in cleared if re.search(rf"\b{re.escape(s)}\b", line)}
    return sorted(hits)
