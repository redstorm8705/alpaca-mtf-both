# ruff: noqa: E501  — dense guard/log strings run long (project convention)
"""
drift_corrector.py — Option C (autonomous): SELF-HEAL an escalated tracker↔broker phantom_tracker
drift mid-session, with NO operator step (Rafael 2026-08-10: "I'm not typing anything to the
terminal ... one blip shouldn't destroy the bot's accounting"). Supersedes the C1 human-confirm
flow (a Slack command the operator had to run) — that offloaded the fix to Rafael; this fixes it
itself, safely.

THE SAFETY KEYSTONE (board APPROVE-WITH-CHANGES + Gro + GAI autonomous-with-guards, 2026-08-10):
the bot drops a phantom ONLY when it can POSITIVELY RECOVER THE REAL CLOSING FILL from Alpaca's
CLOSED-orders/activity feed (execution.fill_helpers.fetch_actual_fill_price_or_none). That single
rule makes every dangerous case safe BY CONSTRUCTION:
  * stale "broker flat" blip on a still-live position -> there is NO close fill -> DO NOTHING.
  * corporate action (symbol rename/split, e.g. FB->META) -> no close fill -> DO NOTHING.
  * position genuinely closed externally -> the real fill exists -> record the HONEST exit price
    and drop the phantom. No operator, no terminal.
The C1 fallback ("broker flat AND no fill found -> drop anyway at entry_price, $0.00 P&L") is
DELETED: it was the HOOD false-drop / masked-loss failure the board flagged. No fill == no action.

GUARDS (all must hold before an autonomous drop):
  G1  Escalated phantom_tracker record only — the detector's DYNAMIC persistence bar has been met
      across cycles (no in-cycle sleeps; reuses drift_detector's cross-cycle persistence map), and
      persistence >= AUTO_HEAL_MIN_PERSISTENCE (a hard floor of 3 cycles, board reliability seat).
  G2  No live/pending order on the symbol (an in-flight fill is the benign explanation — skip).
  G3  POSITIVE close-fill proof: fetch_actual_fill_price_or_none returns a REAL price (side-filtered,
      entry-time-bounded, ±50% sanity band) — else None -> no drop, no fabrication.
  G4  NOT a QHM / Forever-6 anchor (already excluded upstream; re-excluded here belt-and-suspenders).
  G5  Direction != short (RIVN inverted-short scar — shorts stay detect-only / reconcile).
  G6  Notional (tracker qty x entry) <= AUTO_HEAL_MAX_NOTIONAL — a blast-radius cap (board masked-
      loss seat, REQUIRED). Above the cap the phantom is left for the nightly startup reconcile
      (which carries the 120-min settling window + Guard A/B/D) — still zero operator action.

Anything not auto-healed is simply LEFT (the detector already paged it once, informationally); the
existing startup reconcile cleans the rare no-fill / oversized / short cases on the next restart.
Because a phantom means the broker holds NOTHING for the symbol, waiting carries no market risk.

INVARIANTS: never mutates a position without (a) an escalated phantom_tracker record AND (b) a
positively-recovered real close fill. Excludes anchors + shorts. Records HONEST P&L (real fill, not
a fabricated $0.00). Fully fail-safe — never raises into the caller. NOT blocking beyond the bounded
fill query the exit path already uses (poll_secs=0.1, no_retry=True).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# --- v1 safety caps (flagged STATIC; No-Static-Regimes roadmap: derive the notional cap from
# account equity + observed phantom sizes, and drop the persistence floor to the detector's fully-
# dynamic bar, once the auto-heal has a live track record). Both are conservative belt-and-suspenders
# ON TOP of the load-bearing real-fill-proof guard (G3), not the primary safety. ---
AUTO_HEAL_MAX_NOTIONAL = 300.0   # board masked-loss seat: cap a single autonomous drop's blast radius
AUTO_HEAL_MIN_PERSISTENCE = 3    # board reliability seat: >= 3 cross-cycle confirmations before healing

PHANTOM_TRACKER = "phantom_tracker"


def _inflight_symbols(fetch_orders) -> set:
    """Symbols with a live/pending broker order — an in-flight fill is the benign explanation for a
    phantom, so we skip healing them. None/failure -> empty set (fail-open: G3's fill-proof still
    gates the drop, so a missing orders read cannot by itself cause a bad drop)."""
    out: set = set()
    if fetch_orders is None:
        return out
    try:
        for o in (fetch_orders() or []):
            s = o.get("symbol") if isinstance(o, dict) else getattr(o, "symbol", None)
            if s:
                out.add(s)
    except Exception as e:
        logger.warning("auto-heal: open-orders fetch failed (%s) — no in-flight suppression this cycle.", e)
    return out


def auto_heal_phantom_drift(records, tracker, risk, exclude_syms, heal,
                            fetch_orders=None, alert=None,
                            max_notional: float = AUTO_HEAL_MAX_NOTIONAL,
                            min_persistence: int = AUTO_HEAL_MIN_PERSISTENCE) -> int:
    """Autonomously heal ESCALATED phantom_tracker drifts that have a POSITIVELY-RECOVERED real close
    fill. Returns the number healed. NEVER raises. Args injected for testability:
      records        : the list detect_and_emit_drift() returned this cycle (may be None/empty).
      heal(sym,trade)-> pnl|None : recovers the REAL close fill (fetch_actual_fill_price_or_none) and,
                       ONLY if a real price is found, calls tracker.record_exit(alpaca_confirmed_absent
                       =True) and returns the honest pnl; returns None when no real fill exists (the
                       caller then drops NOTHING — the anti-fabrication rule).
      fetch_orders() -> list|None : live broker orders (G2 in-flight suppression).
      alert(msg)     : optional Slack notifier."""
    if not records:
        return 0
    exclude = set(exclude_syms or [])
    inflight = _inflight_symbols(fetch_orders)
    healed = 0
    for r in records:
        try:
            if not isinstance(r, dict) or r.get("drift_type") != PHANTOM_TRACKER:
                continue
            if not r.get("escalate"):
                continue  # G1: dynamic persistence bar not yet met (settle/inflight race)
            sym = r.get("symbol")
            if not sym or sym in exclude:
                continue  # G4: anchor / bad record
            try:
                persist = int(r.get("persistence_cycles", 0))
            except (TypeError, ValueError):
                persist = 0
            if persist < min_persistence:
                continue  # G1: below the >=3-cycle floor
            if sym in inflight:
                logger.info("auto-heal: %s has a live order — in-flight fill likely, skipping.", sym)
                continue  # G2
            tv = r.get("tracker_view") or {}
            if str(tv.get("direction") or "").lower() == "short":
                logger.info("auto-heal: %s is a SHORT phantom — left to reconcile (RIVN scar), not auto-healed.", sym)
                continue  # G5
            try:
                qty = int(tv.get("qty") or 0)
                entry = float(tv.get("entry_price") or 0.0)
            except (TypeError, ValueError):
                logger.warning("auto-heal: %s malformed tracker_view (%r) — skipping.", sym, tv)
                continue
            notional = qty * entry
            if notional > max_notional:
                logger.info("auto-heal: %s notional $%.0f > cap $%.0f — left for startup reconcile (no market risk; broker flat).", sym, notional, max_notional)
                continue  # G6
            trade = (getattr(tracker, "open_trades", {}) or {}).get(sym)
            if not isinstance(trade, dict) or trade.get("status") == "closed":
                continue  # already gone / resolved

            # G3 — the keystone: heal() drops ONLY if it positively recovered the REAL close fill.
            pnl = heal(sym, trade)
            if pnl is None:
                logger.info("auto-heal: %s — no real close fill recovered (broker flat but unproven) -> NOT dropping (anti-fabrication).", sym)
                continue

            if risk is not None:
                try:
                    risk.register_close(pnl or 0.0)
                except Exception as _re:
                    logger.warning("auto-heal: %s register_close failed: %s", sym, _re)
            healed += 1
            msg = (f":white_check_mark: DRIFT AUTO-HEALED — {sym} phantom_tracker dropped "
                   f"(broker flat + real close fill recovered); honest P&L {pnl:+.2f}. No action needed.")
            logger.critical(msg)
            if alert:
                try:
                    alert(msg)
                except Exception as _ae:
                    logger.warning("auto-heal: %s alert failed: %s", sym, _ae)
        except Exception as e:
            # a failure on one symbol must never abort the pass or the cycle — retry next cycle.
            logger.error("auto-heal: %s failed (%s) — skipped, will retry next cycle.", (r.get("symbol") if isinstance(r, dict) else "?"), e)
    return healed
