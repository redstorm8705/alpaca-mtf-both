# ruff: noqa: E501  — dense logger/comment strings run long (project convention; matches execution/broker.py, portfolio_tracker.py, risk_manager.py, etc.)
"""
drift_detector.py — per-cycle READ-ONLY tracker↔broker drift detector (2026-08-09).

Rafael mandate: "Logs from the bot must be visible regardless of when they're being logged or
identified ... dynamic not static in every aspect." Board reliability-scoping + Gro + GAI
UNANIMOUS on this design (Option A). Design doc: logs/reconcile_drift_detector_design_2026-08-09.md.

WHAT: every RTH cycle, compare the intraday tracker's position set (open_trades) to Alpaca's live
/v2/positions and surface any drift IMMEDIATELY — phantom positions, direction/qty mismatch, or a
corrupted entry_price that would later print a $0.00 P&L on a stop. It is the visibility half of the
#4-#7 desync class (the position-set reconcile otherwise runs ONLY at startup).

HARD INVARIANTS (verified by the gate — do not weaken):
  * READ-ONLY. NEVER mutates a position, counter, stop, or P&L. Pure detect + emit. NOT risk-path.
  * FAIL-SAFE. Can never raise into the cycle loop; any error → log + return (the caller also wraps
    it). No blocking I/O beyond a single bounded positions fetch (+ optional orders fetch) placed
    AFTER check_exits(), so it can never delay an exit on the trading thread.
  * EXCLUDES QHM + Forever-6 anchors (legitimately broker-only vs the intraday tracker).

DYNAMIC IN EVERY ASPECT (no static thresholds where a derivation exists):
  * Persistence-based confidence: a 1-cycle mismatch is almost always a settling read / in-flight
    fill → LOW confidence (recorded, not alerted). Confidence RISES with consecutive-cycle
    persistence. The escalation bar is DERIVED from observed self-clearing "race" lifetimes
    (a live percentile), NOT a hard-coded N. A documented fallback bar applies ONLY until enough
    race samples exist (flagged; roadmap to remove per the No-Static-Regimes rule).
  * Each drift emits a structured DriftRecord carrying a `proposed_correction` + `confidence` —
    this is the evidence stream the future auto-corrector (Option C) will consume; A builds C's brain.

THIS MODULE MUTATES NOTHING EXTERNAL. Its only state is an in-memory persistence map + a bounded
race-lifetime history (both reset on process restart, which the startup reconcile already covers).
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_PT = ZoneInfo("America/Los_Angeles")

# Fallback escalation bar (consecutive cycles) used ONLY until >= _MIN_LATENCY_SAMPLES self-clearing
# race lifetimes have been observed. Flagged static — replaced by the derived bar as data accrues.
_FALLBACK_ESCALATION_BAR = 2
_MIN_LATENCY_SAMPLES = 20
_RACE_LIFETIME_MAXLEN = 200          # bounded history of self-clearing drift lifetimes
_RACE_PERSISTENCE_CAP = 6            # a drift that clears within this many cycles counts as a "race"
_ESCALATION_PCTILE = 0.90            # bar = ceil(90th pctile of race lifetimes) + 1

# Drift types
PHANTOM_BROKER = "phantom_broker"        # broker holds it, tracker doesn't
PHANTOM_TRACKER = "phantom_tracker"      # tracker holds it, broker doesn't
DIRECTION_MISMATCH = "direction_mismatch"
QTY_MISMATCH = "qty_mismatch"
ENTRY_CORRUPT = "entry_corrupt"          # tracker entry_price <= 0 → $0-P&L-on-stop risk


def _fresh_state() -> dict:
    """The detector's in-memory state. Kept module-global via get_state()."""
    return {
        # (symbol, drift_type) -> {"count": int, "first_seen": iso, "last_seen": iso}
        "active": {},
        # bounded history of lifetimes (in cycles) of drifts that self-cleared quickly (races)
        "race_lifetimes": deque(maxlen=_RACE_LIFETIME_MAXLEN),
        # (symbol, drift_type) already Slack-alerted this EPISODE — so a persistent drift alerts
        # ONCE, not every cycle (anti-spam; Rafael 2026-08-09). Cleared when the drift resolves,
        # so a drift that clears then re-appears alerts afresh. trade_events.jsonl still records
        # every cycle (full visibility) — only the Slack fires once.
        "alerted": set(),
    }


_STATE = _fresh_state()


def get_state() -> dict:
    return _STATE


def reset_state() -> None:
    """Test hook only."""
    global _STATE
    _STATE = _fresh_state()


def _percentile(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    idx = p * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def escalation_bar(state: dict) -> tuple[int, bool]:
    """Dynamic escalation bar (consecutive cycles) derived from observed race lifetimes.
    Returns (bar, is_dynamic). Falls back to _FALLBACK_ESCALATION_BAR (flagged) until enough
    samples. The bar is always >= 2 (a single-cycle blip is never escalated)."""
    hist = list(state.get("race_lifetimes") or [])
    if len(hist) >= _MIN_LATENCY_SAMPLES:
        bar = int(_percentile(sorted(hist), _ESCALATION_PCTILE)) + 1
        return max(2, bar), True
    return _FALLBACK_ESCALATION_BAR, False


def _tracker_view(open_trades: dict, exclude: set) -> dict:
    """{symbol: {"direction","qty","entry_price"}} for non-closed intraday trades, ex-exclusions."""
    view: dict = {}
    for sym, t in (open_trades or {}).items():
        if not isinstance(t, dict):
            continue
        if sym in exclude:
            continue
        if t.get("status") == "closed":
            continue
        # qty_remaining is the live share count (reduced on partials, 0 on full close)
        qty = t.get("qty_remaining")
        if qty is None:
            qty = t.get("qty")
        try:
            qty_i = int(abs(float(qty or 0)))
        except (TypeError, ValueError):
            qty_i = 0
        if qty_i <= 0:
            continue  # fully-exited residual; not an open position
        try:
            entry = float(t.get("entry_price") or 0.0)
        except (TypeError, ValueError):
            entry = 0.0
        view[sym] = {
            "direction": (t.get("direction") or "").lower(),
            "qty": qty_i,
            "entry_price": entry,
        }
    return view


def _broker_view(alpaca_positions, exclude: set) -> dict:
    """{symbol: {"signed_qty","abs_qty","direction","avg_entry"}} from SDK Position objects OR raw
    /v2/positions dicts (handles both shapes). Excludes the exclusion set."""
    view: dict = {}
    for p in (alpaca_positions or []):
        # support both SDK objects (p.symbol/p.qty) and raw dicts (p["symbol"]/p["qty"])
        if isinstance(p, dict):
            sym = p.get("symbol")
            raw_qty = p.get("qty")
            avg = p.get("avg_entry_price")
        else:
            sym = getattr(p, "symbol", None)
            raw_qty = getattr(p, "qty", None)
            avg = getattr(p, "avg_entry_price", None)
        if not sym or sym in exclude:
            continue
        try:
            signed = float(raw_qty or 0)
        except (TypeError, ValueError):
            continue
        if signed == 0:
            continue
        try:
            avg_f = float(avg or 0.0)
        except (TypeError, ValueError):
            avg_f = 0.0
        view[sym] = {
            "signed_qty": signed,
            "abs_qty": int(abs(signed)),
            "direction": "long" if signed > 0 else "short",
            "avg_entry": avg_f,
        }
    return view


def _classify(tracker_view: dict, broker_view: dict) -> list:
    """Return raw drift dicts (pre-persistence). Each: {symbol, drift_type, tracker_view,
    broker_view, proposed_correction}. proposed_correction = what an auto-corrector WOULD do
    (logged only in this read-only build)."""
    drifts = []
    t_syms = set(tracker_view)
    b_syms = set(broker_view)

    for sym in b_syms - t_syms:
        b = broker_view[sym]
        drifts.append({
            "symbol": sym, "drift_type": PHANTOM_BROKER,
            "tracker_view": None, "broker_view": b,
            "proposed_correction": f"adopt {b['direction']} {b['abs_qty']} @ {b['avg_entry']}",
        })
    for sym in t_syms - b_syms:
        t = tracker_view[sym]
        drifts.append({
            "symbol": sym, "drift_type": PHANTOM_TRACKER,
            "tracker_view": t, "broker_view": None,
            "proposed_correction": "drop tracker entry (record_exit at real fill) — broker flat",
        })
    for sym in t_syms & b_syms:
        t, b = tracker_view[sym], broker_view[sym]
        if t["direction"] and t["direction"] != b["direction"]:
            drifts.append({
                "symbol": sym, "drift_type": DIRECTION_MISMATCH,
                "tracker_view": t, "broker_view": b,
                "proposed_correction": f"flip tracker {t['direction']}→{b['direction']}",
            })
        elif t["qty"] != b["abs_qty"]:
            drifts.append({
                "symbol": sym, "drift_type": QTY_MISMATCH,
                "tracker_view": t, "broker_view": b,
                "proposed_correction": f"resize tracker qty {t['qty']}→{b['abs_qty']}",
            })
        if t["entry_price"] <= 0:
            drifts.append({
                "symbol": sym, "drift_type": ENTRY_CORRUPT,
                "tracker_view": t, "broker_view": b,
                "proposed_correction": f"refetch entry_price (broker avg {b['avg_entry']})",
            })
    return drifts


def detect_drift(open_trades, alpaca_positions, open_orders, exclude_syms, state, now_iso):
    """PURE core. Returns (drift_records, new_state). Never does I/O. Updates persistence + the
    dynamic race-lifetime history.

    open_orders: list of order objects (each with .symbol) OR None (unknown — never used to SUPPRESS
    when None, per the fail-open-on-unknown convention). A live order on a symbol downgrades that
    symbol's drift confidence (an in-flight fill is the benign explanation)."""
    exclude = set(exclude_syms or [])
    tview = _tracker_view(open_trades, exclude)
    bview = _broker_view(alpaca_positions, exclude)
    raw = _classify(tview, bview)

    # symbols with a live/pending order — a drift on one of these is likely an in-flight fill
    inflight = set()
    if open_orders:  # None (unknown) or [] → no suppression
        for o in open_orders:
            osym = o.get("symbol") if isinstance(o, dict) else getattr(o, "symbol", None)
            if osym:
                inflight.add(osym)

    bar, bar_dynamic = escalation_bar(state)
    active = state["active"]
    seen_keys = set()
    records = []

    for d in raw:
        key = (d["symbol"], d["drift_type"])
        seen_keys.add(key)
        prev = active.get(key)
        count = (prev["count"] + 1) if prev else 1
        first_seen = prev["first_seen"] if prev else now_iso
        active[key] = {"count": count, "first_seen": first_seen, "last_seen": now_iso}

        has_inflight = d["symbol"] in inflight
        # confidence rises with persistence; an in-flight order on the symbol caps it low
        raw_conf = min(1.0, count / bar) if bar > 0 else 1.0
        confidence = min(raw_conf, 0.25) if has_inflight else raw_conf
        escalate = (count >= bar) and not has_inflight
        # ANTI-SPAM (Rafael 2026-08-09): Slack fires ONCE per drift episode, not every cycle a
        # persistent drift stays escalated. trade_events still records every cycle. Re-alerts only
        # if the drift clears (key removed from `alerted` below) and later re-appears.
        alerted = state["alerted"]
        slack = escalate and key not in alerted
        if slack:
            alerted.add(key)

        records.append({
            "event": "drift_detected",
            "ts": now_iso,
            "symbol": d["symbol"],
            "drift_type": d["drift_type"],
            "tracker_view": d["tracker_view"],
            "broker_view": d["broker_view"],
            "inflight_order": has_inflight,
            "persistence_cycles": count,
            "escalation_bar": bar,
            "bar_dynamic": bar_dynamic,
            "confidence": round(confidence, 3),
            "escalate": escalate,
            "slack": slack,
            "proposed_correction": d["proposed_correction"],
        })

    # clear resolved drifts; a quick self-clear is a "race" sample that calibrates the dynamic bar
    for key in list(active.keys()):
        if key not in seen_keys:
            lifetime = active[key]["count"]
            if lifetime <= _RACE_PERSISTENCE_CAP:
                state["race_lifetimes"].append(lifetime)
            del active[key]
            state["alerted"].discard(key)  # resolved → a future recurrence alerts afresh

    return records, state


def detect_and_emit_drift(
    tracker,
    exclude_syms,
    log_event,
    send_slack,
    fetch_positions,
    fetch_orders=None,
):
    """FAIL-SAFE I/O wrapper for the cycle loop. Fetches the broker snapshot (bounded), runs the
    pure core, emits EVERY drift to trade_events.jsonl (full visibility) and Slacks ONLY the
    escalated (high-confidence, no-inflight) ones (no alert fatigue). Returns the record list (for
    tests/callers); NEVER raises.

    Args are injected for testability. fetch_positions() -> list; fetch_orders() -> list|None."""
    try:
        alpaca_positions = fetch_positions()
    except Exception as e:
        logger.warning("drift_detector: positions fetch failed (%s) — skipping this cycle.", e)
        return []
    open_orders = None
    if fetch_orders is not None:
        try:
            open_orders = fetch_orders()  # None = unknown → no suppression
        except Exception as e:
            logger.warning("drift_detector: orders fetch failed (%s) — no inflight suppression.", e)
            open_orders = None

    now_iso = datetime.now(_PT).isoformat()
    try:
        open_trades = getattr(tracker, "open_trades", {}) or {}
        records, _ = detect_drift(
            open_trades, alpaca_positions, open_orders, exclude_syms, _STATE, now_iso,
        )
    except Exception as e:
        logger.warning("drift_detector: classify failed (%s) — skipping this cycle.", e)
        return []

    for r in records:
        try:
            # full-visibility evidence stream — every drift, every cycle
            log_event(
                r["event"], symbol=r["symbol"],
                drift_type=r["drift_type"],
                tracker_view=r["tracker_view"], broker_view=r["broker_view"],
                inflight_order=r["inflight_order"],
                persistence_cycles=r["persistence_cycles"],
                escalation_bar=r["escalation_bar"], bar_dynamic=r["bar_dynamic"],
                confidence=r["confidence"], escalate=r["escalate"],
                proposed_correction=r["proposed_correction"],
            )
        except Exception as e:
            logger.warning("drift_detector: log_event failed for %s (%s).", r.get("symbol"), e)
        level = logging.WARNING if r["escalate"] else logging.DEBUG
        logger.log(
            level,
            "DRIFT %s %s | persist=%d/%d conf=%.2f inflight=%s | tracker=%s broker=%s | would: %s",
            r["symbol"], r["drift_type"], r["persistence_cycles"], r["escalation_bar"],
            r["confidence"], r["inflight_order"], r["tracker_view"], r["broker_view"],
            r["proposed_correction"],
        )
        if r.get("slack"):  # fire ONCE per drift episode (anti-spam), not every escalated cycle
            try:
                # Autonomous self-heal (Option C, 2026-08-10): phantom_tracker heals ITSELF when the
                # real closing fill is recovered from Alpaca — NO operator action. Others stay detect-
                # only until their own gated build. Informational only: never asks Rafael to do anything.
                if r["drift_type"] == "phantom_tracker":
                    _fix = ("The bot will AUTO-HEAL this itself once it recovers the real closing fill "
                            "from Alpaca (or leaves it for the nightly reconcile). No action needed.")
                else:
                    _fix = "Auto-correct not yet enabled for this drift type (detect-only)."
                send_slack(
                    f":warning: TRACKER↔BROKER DRIFT — {r['symbol']} {r['drift_type']} "
                    f"(persisted {r['persistence_cycles']} cycles, conf {r['confidence']:.2f}). "
                    f"tracker={r['tracker_view']} broker={r['broker_view']}. "
                    f"Would: {r['proposed_correction']}. {_fix}"
                )
            except Exception as e:
                logger.warning("drift_detector: slack failed for %s (%s).", r.get("symbol"), e)

    return records
