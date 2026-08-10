# ruff: noqa: E501  — dense guard/log strings run long (project convention)
"""
drift_corrector.py — Option C1: apply an OPERATOR-CONFIRMED tracker↔broker drift correction
mid-session (Rafael 2026-08-09; BGG unanimous — human-confirmed ONLY, no unattended set-mutation).

The drift detector SURFACES drift; it NEVER corrects. This module applies a correction ONLY when the
operator has explicitly authorized THAT specific drift via confirm_drift_correction.py, AND the live
tracker+broker snapshot STILL exactly matches what was confirmed. Every unattended set-mutation has
caused an incident (HOOD false-drop on a stale read; RIVN inverted-short adoption), so this fails
CLOSED on any mismatch: a confirmation whose snapshot has moved is consumed and re-paged, never
applied to a changed situation.

SCOPE (v1): only `phantom_tracker` (tracker holds a position the broker does not) is auto-applied on
confirmation — the SNOW/PANW mid-RTH phantom case. It drops the stale tracker entry by recording the
real external-close exit via the SAME hardened path the startup reconcile uses (fetch_actual_fill_
price + tracker.record_exit(alpaca_confirmed_absent=True) + risk.register_close). Other drift types
(phantom_broker adopt / direction flip / qty resize) are NOT applied here yet — they stay
detect-and-page until their own gated build. A confirmation for an unsupported type is left intact
(not consumed) so nothing is silently dropped.

INVARIANTS: never mutates a position without (a) a matching unexpired one-shot confirmation AND (b) a
live snapshot that still matches the confirmation exactly. Excludes QHM+Forever-6 anchors. Fully
fail-safe — never raises into the caller.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_CONF = _ROOT / "data" / "state" / "drift_correction_confirmations.json"
_TTL_SEC = 2 * 3600   # confirmations expire after 2h (mirror the ledger-heal window)


def _load_confs() -> dict:
    try:
        if _CONF.exists():
            d = json.loads(_CONF.read_text())
            return d if isinstance(d, dict) else {}
    except Exception as e:
        logger.warning("drift_corrector: confirmations unreadable (%s) — none applied.", e)
    return {}


def _save_confs(confs: dict) -> None:
    try:
        _CONF.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CONF.with_suffix(f".json.{os.getpid()}.tmp")
        with open(tmp, "w") as f:
            json.dump(confs, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(_CONF)
    except Exception as e:
        logger.error("drift_corrector: failed to persist confirmations (%s).", e)


def _tracker_abs_qty(tracker, sym: str) -> int:
    t = (getattr(tracker, "open_trades", {}) or {}).get(sym)
    if not isinstance(t, dict) or t.get("status") == "closed":
        return 0
    q = t.get("qty_remaining")
    if q is None:
        q = t.get("qty")
    try:
        return int(abs(float(q or 0)))
    except (TypeError, ValueError):
        return 0


def _broker_abs_qty(positions, sym: str) -> int:
    for p in (positions or []):
        psym = p.get("symbol") if isinstance(p, dict) else getattr(p, "symbol", None)
        if psym == sym:
            raw = p.get("qty") if isinstance(p, dict) else getattr(p, "qty", None)
            try:
                return int(abs(float(raw or 0)))
            except (TypeError, ValueError):
                return 0
    return 0


def apply_confirmed_corrections(tracker, risk, exclude_syms, fetch_positions,
                                record_exit_drop, alert=None) -> int:
    """Apply operator-confirmed drift corrections whose live snapshot STILL matches. Returns the
    number applied. NEVER raises. Args injected for testability:
      fetch_positions() -> list of live broker positions (or None on failure -> apply nothing).
      record_exit_drop(sym, trade) -> pnl : drops the tracker's phantom entry via the hardened
        external-close path (fetch real fill -> record_exit(alpaca_confirmed_absent=True)).
      alert(msg) : optional Slack notifier."""
    confs = _load_confs()
    if not confs:
        return 0
    try:
        positions = fetch_positions()
    except Exception as e:
        logger.warning("drift_corrector: broker fetch failed (%s) — applying nothing this cycle.", e)
        return 0
    if positions is None:
        logger.warning("drift_corrector: broker positions unknown (None) — applying nothing (fail-closed).")
        return 0

    exclude = set(exclude_syms or [])
    now = time.time()
    applied = 0
    changed = False
    for key in list(confs.keys()):
        c = confs.get(key) or {}
        sym = c.get("symbol")
        dtype = c.get("drift_type")
        if not sym or not dtype:
            del confs[key]
            changed = True
            continue
        if sym in exclude:
            # a QHM/F6 anchor must never be corrected by this path — drop the confirmation.
            logger.warning("drift_corrector: %s is a protected anchor — refusing correction, consuming token.", sym)
            del confs[key]
            changed = True
            continue
        if now - float(c.get("confirmed_ts", 0) or 0) > _TTL_SEC:
            logger.info("drift_corrector: %s/%s confirmation expired (>2h) — consumed, not applied.", sym, dtype)
            del confs[key]
            changed = True
            continue

        live_tracker = _tracker_abs_qty(tracker, sym)
        live_broker = _broker_abs_qty(positions, sym)
        try:
            # NOTE: dict.get's default only applies to a MISSING key, not a JSON null value — so
            # int(c.get(...)) would TypeError on a null. Convert defensively and consume a malformed
            # confirmation rather than crash the whole apply pass.
            exp_tracker = int(c.get("tracker_at_confirm"))
            exp_broker = int(c.get("broker_at_confirm"))
        except (TypeError, ValueError):
            logger.warning("drift_corrector: %s/%s malformed confirmation (bad qty) — consumed.", sym, dtype)
            del confs[key]
            changed = True
            continue
        # FAIL-CLOSED snapshot re-verify: if the live situation moved since the operator confirmed,
        # DO NOT apply — consume the token and page (a moved snapshot means the situation changed).
        if live_tracker != exp_tracker or live_broker != exp_broker:
            msg = (f":warning: DRIFT CORRECTION SKIPPED — {sym}/{dtype}: live tracker={live_tracker} "
                   f"broker={live_broker} moved since you confirmed (tracker={exp_tracker} broker={exp_broker}). "
                   "Token consumed; re-confirm from the current alert if still needed.")
            logger.warning(msg)
            if alert:
                try:
                    alert(msg)
                except Exception as _ae:
                    logger.warning("drift_corrector: alert failed: %s", _ae)
            del confs[key]
            changed = True
            continue

        if dtype != "phantom_tracker":
            # only phantom_tracker is applied in v1; leave others intact (do NOT consume) so they
            # are not silently dropped — they await their own gated build.
            logger.info("drift_corrector: %s/%s not yet supported for auto-apply — left pending.", sym, dtype)
            continue

        # phantom_tracker: tracker holds it, broker does not -> drop the stale tracker entry by
        # recording the real external-close exit (the same hardened path the startup reconcile uses).
        trade = (getattr(tracker, "open_trades", {}) or {}).get(sym)
        if not isinstance(trade, dict):
            logger.info("drift_corrector: %s no live tracker entry — nothing to drop, consuming token.", sym)
            del confs[key]
            changed = True
            continue
        try:
            pnl = record_exit_drop(sym, trade)
            if pnl is None:
                # record_exit early-returned WITHOUT dropping (entry already gone / qty<=0). Do NOT
                # consume the token — the next cycle's snapshot re-verify (live_tracker now != the
                # confirmed value) will clean it up. Prevents consuming a token on a no-op.
                logger.warning("drift_corrector: %s record_exit returned None (no drop) — token kept for retry.", sym)
                continue
            if risk is not None:
                try:
                    risk.register_close(pnl or 0.0)
                except Exception as _re:
                    logger.warning("drift_corrector: %s register_close failed: %s", sym, _re)
            applied += 1
            del confs[key]
            changed = True
            msg = (f":white_check_mark: DRIFT CORRECTED (operator-confirmed) — {sym} phantom_tracker "
                   f"dropped (broker flat); exit recorded, P&L {pnl if pnl is not None else 'unverified'}.")
            logger.critical(msg)
            if alert:
                try:
                    alert(msg)
                except Exception as _ae:
                    logger.warning("drift_corrector: alert failed: %s", _ae)
        except Exception as e:
            # a drop failure must NOT consume the token — leave it so a later cycle can retry.
            logger.error("drift_corrector: %s phantom_tracker drop FAILED (%s) — token kept for retry.", sym, e)

    if changed:
        _save_confs(confs)
    return applied
