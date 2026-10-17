#!/usr/bin/env python3
# ruff: noqa: E501
"""
execution/forever_hold_manager.py — F6 cash-only accumulation tier (disabled pending launch approval).

The BGG-locked STARTER rule (Rafael 2026-07-13, logs/f6_starter_bgg_2026-07-13.md): on a market-wide
dip (SPY down >= a DYNAMIC threshold on the close), ESTABLISH starter positions in 1-3 Forever-6 anchor
names. This tier is CASH-ONLY (the cold board proved margin makes a never-sell book hostage to any other
strategy's worst day — a maintenance call would force-liquidate the anchors), breadth-first, catalyst-
SCREENED (skip a name with an active negative catalyst — the RIVN lesson, now enforced by the live
catalyst_engine gate), and funded from a SEGREGATED budget that can never cannibalize the deep crash
ladder's dry powder.

The planner and bounded order path are wired behind FOREVER6_ENABLED.  The tier remains disabled until
the remaining launch prerequisites and the owner approval package are complete.  The planner itself
never places an order; execute_starter is the money-moving boundary.
"""
from __future__ import annotations

import hashlib
import contextlib
import fcntl
import json
import logging
import math
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable, cast
from zoneinfo import ZoneInfo

import config
from execution.f6_empirical_trigger import evaluate_empirical_starter
from execution.instrument_policy import f6_live_asset_eligible, f6_symbol_eligible

logger = logging.getLogger("forever6")

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
_ROOT = Path(__file__).resolve().parent.parent
_STATE = _ROOT / "data" / "state" / "forever6_holds.json"
# C-3 (design logs/f6_prereq1_syncgap_design_2026-07-17.md): persisted "block further F6
# seeding" flag. Set when a post-buy ledger sync fails to reflect a fresh F6 lot (C-2);
# blocks the NEXT seed until a clean sync clears it. Scoped to SEEDING only — never a
# sell/exit path. Fail-CLOSED on read error (treat as degraded → block), the safe
# direction for a never-sell book.
_SYNC_DEGRADED = _ROOT / "data" / "state" / "forever6_sync_degraded.json"
# Per-symbol trim-rung state (F6 exit increment, 2026-08-05): {SYM: {"trim1": "none"|
# "reserved"|"done", "trim2": ...}}. Durable across restarts so a 10x/20x trim, once
# reserved or executed, can never re-fire on the same rung. Separate from _STATE (the
# starter-event log) — different shape, different write cadence (once per
# reserve/confirm transition vs. once per starter event).
_TRIM_STATE = _ROOT / "data" / "state" / "forever6_trims.json"
_TRIGGER_EVIDENCE = _ROOT / "data" / "state" / "forever6_trigger_evidence.jsonl"
_STATE_LOCK = _ROOT / "data" / "state" / ".forever6_holds.lock"
_EPS = 1e-6
# Process-local half of the startup interlock.  Durable state protects restarts;
# this boolean also fails closed if persisting that state itself fails.
_STARTUP_READY = False
_F6_ENTRY_START = time.fromisoformat("16:05")
_F6_ENTRY_END = time.fromisoformat("20:00")
_F6_QUOTE_OBSERVATIONS: dict[str, datetime] = {}
_F6_QUOTE_OBSERVATIONS_LOCK = threading.Lock()


@contextlib.contextmanager
def _state_locked():
    """Serialize the F6 event journal across bot processes."""
    _STATE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(_STATE_LOCK, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class F6StartupReconcileResult:
    """Exact startup ownership verdict for the F6 order path.

    ``ready`` is deliberately narrow: it means the full tagged-fill replay healed,
    every F6 ledger claim agrees with the broker net, and no F6-tagged order is
    still open.  It does not enable the tier; the launch flag remains a separate,
    fully gated change.
    """

    ready: bool
    symbols_reconciled: tuple[str, ...] = ()
    pending_order_ids: tuple[str, ...] = ()
    reason: str = ""


class _TrimStateUnreadable(Exception):
    """Raised by _load_trim_state when forever6_trims.json EXISTS but can't be parsed —
    distinct from a legitimately-absent file (which means 'no trim ever recorded' and is
    safe to treat as {}). Must propagate to skip trim evaluation, never be swallowed and
    treated as empty state."""


def _slack_safe(msg: str) -> None:
    """Best-effort Slack; never raises into the F6 path."""
    try:
        from alerts import send_slack
        send_slack(msg)
    except Exception as e:  # RC-3: logged, not swallowed silently
        logger.warning("forever6: slack alert failed: %s", e)


def _read_sync_degraded() -> bool:
    """True → F6 seeding is blocked. Fail-CLOSED: an unreadable flag returns True (block),
    the safe direction for a never-sell book (never seed on uncertainty)."""
    try:
        if _SYNC_DEGRADED.exists():
            d = json.loads(_SYNC_DEGRADED.read_text())
            return bool(d.get("degraded", False))
    except Exception as e:
        logger.warning("forever6: sync-degraded read failed — fail-closed (block): %s", e)
        return True
    return False


def _write_sync_degraded(degraded: bool, detail: str = "") -> bool:
    """Atomic tmp→fsync→replace (RC-5). A write failure is logged LOUDLY: if we meant to
    SET degraded and the write fails, the next seed may proceed unblocked — so escalate."""
    try:
        _SYNC_DEGRADED.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SYNC_DEGRADED.with_suffix(f".json.{os.getpid()}.tmp")
        with open(tmp, "w") as f:
            json.dump({"degraded": bool(degraded), "detail": detail,
                       "ts": datetime.now(PT).isoformat()}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(_SYNC_DEGRADED))
        return True
    except Exception as e:
        logger.error("forever6: sync-degraded write failed (degraded=%s): %s", degraded, e)
        if degraded:
            _slack_safe(f":rotating_light: F6 could not persist sync-degraded flag ({e}) "
                        f"— next seed may NOT be auto-blocked; verify manually.")
        return False

def _f6_entry_session(now_et: datetime) -> bool:
    """F6 starters may submit only while Alpaca accepts extended-hours DAY orders."""
    local_time = now_et.timetz().replace(tzinfo=None)
    return now_et.weekday() < 5 and _F6_ENTRY_START <= local_time < _F6_ENTRY_END


def _quote_time(value: object) -> datetime | None:
    """Parse Alpaca's RFC3339 timestamp, including nanosecond fractions on Python 3.10."""
    raw = str(value or "")
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    if "." in raw:
        head, tail = raw.split(".", 1)
        split_at = next((i for i, char in enumerate(tail) if char in "+-"), len(tail))
        raw = f"{head}.{tail[:split_at][:6]}{tail[split_at:]}"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _validated_quote(raw: object, now_et: datetime) -> tuple[float, float, datetime] | None:
    """Return a structurally valid quote from the active ET session."""
    if not isinstance(raw, dict):
        return None
    try:
        bid = float(raw.get("bid", 0) or 0)
        ask = float(raw.get("ask", 0) or 0)
    except (TypeError, ValueError):
        return None
    quote_ts = _quote_time(raw.get("ts"))
    quote_et = quote_ts.astimezone(ET) if quote_ts is not None else None
    if (not all(math.isfinite(value) and value > 0 for value in (bid, ask))
            or bid > ask or quote_et is None or quote_et > now_et
            or quote_et.date() != now_et.date()
            or quote_et.timetz().replace(tzinfo=None) < _F6_ENTRY_START):
        return None
    return bid, ask, quote_et


def _observe_advancing_quote(
    symbol: str, raw: object, observed_at: datetime,
) -> tuple[float, float, datetime] | None:
    """Prove feed liveness across cycles without a static quote-age threshold.

    The first current-session observation arms the symbol but cannot authorize a
    buy.  A later cycle must observe a strictly newer exchange timestamp.  An
    unchanged 4:05 PM quote therefore remains blocked at 7:59 PM, while ordinary
    REST calls do not depend on a quote changing within a few milliseconds.
    """
    confirmed, _ = _observe_advancing_basket(
        {symbol: _validated_quote(raw, observed_at)}
    )
    return confirmed.get(symbol)


def _observe_advancing_basket(
    observations: dict[str, tuple[float, float, datetime] | None],
) -> tuple[dict[str, tuple[float, float, datetime]], set[str]]:
    """Atomically advance a complete basket's monotonic quote watermarks.

    Missing/current-day-first observations seed only their own watermark.  A
    regression never lowers it.  If any name is blocked, otherwise-valid names
    retain their prior watermark so a sparse symbol cannot force every basket
    member to print again in the same later runner cycle.
    """
    blocked: set[str] = set()
    with _F6_QUOTE_OBSERVATIONS_LOCK:
        for symbol, quote in observations.items():
            if quote is None:
                blocked.add(symbol)
                continue
            prior = _F6_QUOTE_OBSERVATIONS.get(symbol)
            if (prior is None
                    or prior.astimezone(ET).date() != quote[2].astimezone(ET).date()):
                _F6_QUOTE_OBSERVATIONS[symbol] = quote[2]
                blocked.add(symbol)
            elif quote[2] <= prior:
                blocked.add(symbol)
        if blocked:
            return {}, blocked
        confirmed = {
            symbol: quote
            for symbol, quote in observations.items()
            if quote is not None
        }
        for symbol, quote in confirmed.items():
            _F6_QUOTE_OBSERVATIONS[symbol] = quote[2]
        return confirmed, set()


class ForeverHoldManager:
    """F6 never-sell accumulation, disabled until the launch gate is approved."""

    def __init__(
        self,
        broker,
        *,
        clock: Callable[[], datetime] | None = None,
        quote_loader: Callable[[str], dict | None] | None = None,
    ) -> None:
        self.broker = broker
        self.universe = list(config.FOREVER6_UNIVERSE)
        self.clock = clock or (lambda: datetime.now(ET))
        if quote_loader is None:
            from data.alpaca_data import get_latest_quote
            quote_loader = get_latest_quote
        self.quote_loader = quote_loader

    def reconcile_on_startup(self) -> F6StartupReconcileResult:
        """Rebuild and verify F6 ownership before any F6 action after restart.

        The full ledger replay joins fills to exact F6 client-order IDs.  A clean
        replay alone is insufficient: an accepted/part-filled order can still fill
        later, so any open F6-tagged order makes ownership unresolved.  On every
        ambiguity this method persists the existing sync-degraded interlock, which
        blocks F6 starters and trims until a later clean startup/preflight clears it.

        This method never raises into ``main``.  It sends no orders and never edits
        a tier claim directly; ``run_ledger_sync.sync_once`` remains the one replay
        authority.
        """

        global _STARTUP_READY
        _STARTUP_READY = False
        try:
            import run_ledger_sync as _rls
            from execution import ownership_guard as _og

            sync = _rls.sync_once()
            if not sync.get("ok") or not sync.get("healed"):
                return self._startup_reconcile_failed(
                    f"full ledger replay failed: {sync.get('reason') or sync.get('error') or sync}"
                )

            from execution.pending_protected_buys import load as _load_pending_f6
            _pending_rows = [
                row for row in _load_pending_f6()["reservations"].values()
                if row["status"] in {"reserved", "submitted"}
            ]
            if _pending_rows:
                return self._startup_reconcile_failed(
                    "F6 protected-buy reservation(s) unresolved",
                    pending=tuple(sorted(row["client_order_id"] for row in _pending_rows)),
                )

            open_orders = self.broker.get_open_orders()
            if open_orders is None:
                return self._startup_reconcile_failed("open-order read failed")

            pending: list[str] = []
            for order in open_orders:
                coid = getattr(order, "client_order_id", None)
                if coid is None and isinstance(order, dict):
                    coid = order.get("client_order_id")
                if _og.tier_of_coid(coid) != "forever6":
                    continue
                oid = getattr(order, "id", None)
                if oid is None and isinstance(order, dict):
                    oid = order.get("id")
                pending.append(str(oid or coid or "unknown"))
            if pending:
                return self._startup_reconcile_failed(
                    "F6-tagged order(s) still open",
                    pending=tuple(sorted(pending)),
                )

            ledger = _og.load_ledger()
            reconciled: list[str] = []
            for symbol, entry in ledger.get("positions", {}).items():
                own = _og.tier_qty(ledger, symbol, "forever6")
                if own <= _EPS:
                    continue
                net = float(entry.get("alpaca_net_qty", 0.0) or 0.0)
                drift = float(entry.get("drift", 0.0) or 0.0)
                if (
                    not math.isfinite(own)
                    or not math.isfinite(net)
                    or not math.isfinite(drift)
                    or own < -_EPS
                    or abs(own - round(own)) > _EPS
                    or net < own - _EPS
                    or abs(drift) > _EPS
                ):
                    return self._startup_reconcile_failed(
                        f"ownership mismatch {symbol}: f6={own} net={net} drift={drift}"
                    )
                reconciled.append(str(symbol).upper())

            if not _write_sync_degraded(False, "startup ownership reconciled"):
                return self._startup_reconcile_failed(
                    "could not persist the clean startup ownership verdict"
                )
            _STARTUP_READY = True
            result = F6StartupReconcileResult(
                ready=True,
                symbols_reconciled=tuple(sorted(reconciled)),
                reason="ok",
            )
            logger.info(
                "[F6] startup ownership reconcile OK: %d symbol(s), no pending F6 orders",
                len(result.symbols_reconciled),
            )
            return result
        except Exception as exc:
            return self._startup_reconcile_failed(f"unexpected startup error: {exc}")

    def _startup_reconcile_failed(
        self,
        reason: str,
        pending: tuple[str, ...] = (),
    ) -> F6StartupReconcileResult:
        """Persist and report an ownership ambiguity without ever raising."""

        logger.critical("[F6] startup ownership reconcile BLOCKED: %s", reason)
        _write_sync_degraded(True, f"startup reconcile: {reason}")
        _slack_safe(
            ":rotating_light: F6 startup ownership reconcile BLOCKED — "
            f"{reason}. F6 orders remain disabled until a clean reconcile."
        )
        return F6StartupReconcileResult(
            ready=False,
            pending_order_ids=pending,
            reason=reason,
        )

    # ── state (per-month event cap) ────────────────────────────────────────────
    def _load_state(self) -> dict:
        try:
            if _STATE.exists():
                d = json.loads(_STATE.read_text())
                if isinstance(d, dict):
                    return d
        except Exception as e:
            logger.warning("forever6: state read failed: %s", e)
        return {"events": []}

    @staticmethod
    def _load_state_strict() -> dict:
        """Execution-boundary state read; corruption blocks rather than looking empty."""
        if not _STATE.exists():
            return {"events": []}
        try:
            state = json.loads(_STATE.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"F6 event state unreadable: {exc}") from exc
        if not isinstance(state, dict) or not isinstance(state.get("events"), list):
            raise RuntimeError("F6 event state is malformed")
        return state

    def _events_this_month(self, state: dict) -> int:
        ym = datetime.now(PT).strftime("%Y-%m")
        return sum(
            1
            for e in state.get("events", [])
            if isinstance(e, dict)
            and str(e.get("date", "")).startswith(ym)
            and e.get("status") != "released_no_order"
        )

    @staticmethod
    def _event_counts(event: object) -> bool:
        return isinstance(event, dict) and event.get("status") != "released_no_order"

    # ── starter evaluation (returns a plan; execute_starter owns order placement) ──
    def maybe_start_accumulation(
        self,
        spy_daily_bars,
        expected_session_date,
        price_by_sym: dict[str, float] | None = None,
        held_qty_by_sym: dict[str, float] | None = None,
        settled_cash: float | None = None,
    ) -> dict:
        """Evaluate the F6 starter from completed SPY empirical distributions.
        Returns {"triggered": bool, "reason": str, "plan": [{symbol, price, why}], "budget": float}."""
        verdict = evaluate_empirical_starter(
            spy_daily_bars,
            expected_session_date=expected_session_date,
            monthly_event_cap=config.FOREVER6_STARTER_MAX_EVENTS_PER_MONTH,
        )
        evidence = verdict.evidence()
        if not self._append_trigger_evidence(evidence):
            return {"triggered": False, "reason": "trigger evidence write failed", "plan": [],
                    "budget": 0.0, "trigger_evidence": evidence}
        if not verdict.armed:
            return {"triggered": False, "reason": verdict.reason, "plan": [],
                    "budget": 0.0, "trigger_evidence": evidence}

        state = self._load_state_strict()
        # Durable per-DAY idempotency: if a starter event was already recorded today, do NOT
        # place again. This is the definitive anti-double-place guard for the run_cycle after-close
        # hook, which re-evaluates every closed-market cycle (and could otherwise re-fire after a
        # mid-evening restart). State-based → survives restarts, unlike an in-process flag.
        _today_str = datetime.now(PT).strftime("%Y-%m-%d")
        if any(self._event_counts(e) and str(e.get("date", "")) == _today_str
               for e in state.get("events", [])):
            return {"triggered": True, "reason": f"already ran today ({_today_str})", "plan": [], "budget": 0.0}
        n_month = self._events_this_month(state)
        if n_month >= config.FOREVER6_STARTER_MAX_EVENTS_PER_MONTH:
            return {"triggered": True, "reason": f"per-month cap hit ({n_month}/{config.FOREVER6_STARTER_MAX_EVENTS_PER_MONTH})", "plan": [], "budget": 0.0}

        # Cash-only segregated budget: min(frac×cash, cash − floor), never negative.
        cash = float(settled_cash) if settled_cash is not None else self._fetch_cash()
        budget = min(config.FOREVER6_STARTER_CASH_FRAC_PER_EVENT * cash,
                     cash - config.FOREVER6_STARTER_CASH_FLOOR)
        if budget <= 0:
            return {"triggered": True, "reason": f"insufficient segregated cash (cash ${cash:.0f}, floor ${config.FOREVER6_STARTER_CASH_FLOOR:.0f})", "plan": [], "budget": 0.0}

        prices = price_by_sym or self._fetch_prices()
        held = held_qty_by_sym or {}

        # Candidate filter: catalyst screen (live gate) + affordable within remaining budget.
        try:
            import events.catalyst_engine as _cat
        except Exception:
            _cat = None  # type: ignore[assignment]
        candidates: list[tuple[str, float, int]] = []
        for sym in self.universe:
            eligible, policy_reason = f6_symbol_eligible(sym, config.FOREVER6_UNIVERSE)
            if not eligible:
                logger.error("[F6] %s SKIPPED — instrument policy: %s", sym, policy_reason)
                continue
            px = prices.get(sym)
            if not px or px <= 0:
                continue
            if _cat is not None and _cat.has_blocking_catalyst(sym):
                logger.info("[F6] %s SKIPPED — active negative catalyst (screen)", sym)
                continue
            # held = FOREVER-6-TIER holdings only (0 for all until the tier is established) — NOT
            # QHM/intraday shares; the F6 starter builds the F6 base independently.
            candidates.append((sym, px, int(held.get(sym, 0) or 0)))

        # Breadth-first: F6-tier-0 names first (establish the base), then cheapest (fit more names).
        candidates.sort(key=lambda c: (c[2] > 0, c[1]))

        plan: list[dict] = []
        remaining = budget
        for sym, px, held_q in candidates:
            if len(plan) >= config.FOREVER6_STARTER_MAX_NAMES:
                break
            if px <= remaining:   # cash-only: only if 1 share fits the remaining segregated budget
                plan.append({"symbol": sym, "price": round(px, 2),
                             "why": ("new base" if held_q == 0 else f"add (held {held_q})")})
                remaining -= px

        # Planning is side-effect free with respect to the broker; the caller may
        # pass the plan to execute_starter only after every outer gate passes.
        if plan:
            logger.warning("[F6] STARTER would ACCUMULATE on SPY %+.2f%% "
                           "(cutoff %+.2f%%; volume stress %.2fx >= %.2fx): budget $%.0f → %s",
                           verdict.current_return_pct, verdict.return_cutoff_pct,
                           verdict.current_stress_ratio, verdict.stress_cutoff_ratio, budget,
                           ", ".join(f"{p['symbol']}@${p['price']}" for p in plan))
        else:
            logger.info("[F6] STARTER triggered but no fundable name (budget $%.0f) — screen/affordability filtered all.", budget)
        return {"triggered": True, "reason": "ok", "plan": plan, "budget": round(budget, 2),
                "trigger_evidence": evidence}

    def _append_trigger_evidence(self, evidence: dict) -> bool:
        """Append exact decision evidence; suppress identical repeated polls."""
        try:
            prior = None
            if _TRIGGER_EVIDENCE.exists():
                with open(_TRIGGER_EVIDENCE, encoding="utf-8") as existing:
                    for line in existing:
                        if line.strip():
                            prior = json.loads(line)
            row = {"recorded_at": datetime.now(PT).isoformat(), **evidence}
            keys = ("as_of_date", "prior_record_sha256", "armed", "reason")
            if prior and all(prior.get(key) == row.get(key) for key in keys):
                return True
            _TRIGGER_EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
            with open(_TRIGGER_EVIDENCE, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            logger.info(
                "[F6] change_id=f6-empirical-starter as_of=%s armed=%s history=%s",
                evidence.get("as_of_date"),
                evidence.get("armed"),
                evidence.get("history_observations"),
            )
            return True
        except Exception as exc:
            logger.error("[F6] trigger evidence append failed — starter blocked: %s", exc)
            return False

    def _trigger_evidence_is_current(self, evidence: dict | None) -> bool:
        """Require the exact armed decision that was durably appended this ET session."""
        try:
            if not isinstance(evidence, dict):
                return False
            if type(evidence.get("schema_version")) is not int:  # bool is not a version
                return False
            if evidence.get("schema_version") != 1 or evidence.get("armed") is not True:
                return False
            as_of = date.fromisoformat(str(evidence.get("as_of_date", "")))
            if as_of != datetime.now(ET).date():
                return False
            history_start = date.fromisoformat(str(evidence.get("history_start", "")))
            history_end = date.fromisoformat(str(evidence.get("history_end", "")))
            if history_start > history_end or history_end >= as_of:
                return False
            observations = evidence.get("history_observations")
            if type(observations) is not int or observations <= 0:
                return False
            raw_numbers = (
                evidence.get("tail_probability"),
                evidence.get("current_return_pct"),
                evidence.get("return_cutoff_pct"),
                evidence.get("current_stress_ratio"),
                evidence.get("stress_cutoff_ratio"),
            )
            if any(
                not isinstance(value, (int, float)) or isinstance(value, bool)
                for value in raw_numbers
            ):
                return False
            tail, current_return, return_cutoff, current_stress, stress_cutoff = (
                float(cast(int | float, value)) for value in raw_numbers
            )
            if not all(math.isfinite(value) for value in (
                tail, current_return, return_cutoff, current_stress, stress_cutoff,
            )):
                return False
            if not 0 < tail < 1:
                return False
            if current_return > return_cutoff or current_stress < stress_cutoff:
                return False
            digest = str(evidence.get("prior_record_sha256", ""))
            if len(bytes.fromhex(digest)) != hashlib.sha256().digest_size:
                return False
            prior = None
            with open(_TRIGGER_EVIDENCE, encoding="utf-8") as existing:
                for line in existing:
                    if line.strip():
                        prior = json.loads(line)
            if not isinstance(prior, dict):
                return False
            prior.pop("recorded_at", None)
            return prior == evidence
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    # ── the starter EXECUTION (1b: CASH-ONLY orders, fail-closed) ────────────────
    def execute_starter(self, plan: list[dict], budget: float,
                        settled_cash: float | None = None,
                        trigger_evidence: dict | None = None) -> dict:
        """Increment 1b: place CASH-ONLY market BUY orders for a starter PLAN (1 share per planned
        name), tier-tagged "forever6" so the ownership ledger protects them as never-sell shares.
        NEVER sells. FAIL-CLOSED: stops on the FIRST broker failure and never counts an unconfirmed
        order as placed (never-mask — a data/broker fault must not manufacture or hide a fill).

        The money-moving boundary independently enforces the approved weekday
        4:05–8:00 PM ET extended-hours session.
        Returns {"placed": [...], "skipped": [...], "spent": float, "reason": str}."""
        if not plan:
            return {"placed": [], "skipped": [], "spent": 0.0, "reason": "empty plan"}

        # The master flag is enforced again at the money-moving boundary.  A
        # direct or future caller cannot bypass run_cycle's outer DARK gate.
        # Trims use their separate sell-only method and remain reachable.
        if not config.FOREVER6_ENABLED:
            return {
                "placed": [],
                "skipped": list(plan),
                "spent": 0.0,
                "reason": "F6 disabled",
            }

        if not _STARTUP_READY:
            logger.critical(
                "[F6] execute_starter BLOCKED — this process has not passed the exact "
                "startup ownership preflight."
            )
            return {
                "placed": [],
                "skipped": list(plan),
                "spent": 0.0,
                "reason": "startup ownership preflight not ready",
            }

        boundary_now = self.clock().astimezone(ET)
        if not _f6_entry_session(boundary_now):
            return {"placed": [], "skipped": list(plan), "spent": 0.0,
                    "reason": "outside F6 entry session"}
        if len(plan) > config.FOREVER6_STARTER_MAX_NAMES:
            return {"placed": [], "skipped": list(plan), "spent": 0.0,
                    "reason": "starter name cap exceeded"}

        # C-3 (design 2026-07-17): refuse to place if a prior post-buy sync left the ledger
        # degraded (a fresh F6 lot unreflected). Blocks a BUY only — never a sell — the safe
        # direction for a never-sell book. Auto-cleared when a later sync verifies clean (C-2).
        if _read_sync_degraded():
            logger.critical("[F6] execute_starter BLOCKED — prior F6 sync degraded (a fresh "
                            "lot was left unreflected in the ledger). Not placing; resolve first.")
            _slack_safe(":rotating_light: F6 seed BLOCKED — prior sync degraded; further F6 "
                        "buys held until a clean ledger sync clears it.")
            return {"placed": [], "skipped": list(plan), "spent": 0.0,
                    "reason": "sync-degraded block"}

        from execution import pending_protected_buys as _pending_f6
        try:
            if any(
                row.get("status") in _pending_f6.ACTIVE
                for row in _pending_f6.load()["reservations"].values()
            ):
                return {"placed": [], "skipped": list(plan), "spent": 0.0,
                        "reason": "prior protected buy unresolved"}
        except Exception as exc:
            logger.critical("[F6] pending-buy journal unreadable — starter blocked: %s", exc)
            return {"placed": [], "skipped": list(plan), "spent": 0.0,
                    "reason": "protected-buy journal unreadable"}

        try:
            state = self._load_state_strict()
        except Exception as exc:
            logger.critical("[F6] event state unreadable — starter blocked: %s", exc)
            return {"placed": [], "skipped": list(plan), "spent": 0.0,
                    "reason": "event state unreadable"}
        today_pt = datetime.now(PT).strftime("%Y-%m-%d")
        if any(self._event_counts(event) and str(event.get("date", "")) == today_pt
               for event in state.get("events", [])):
            return {"placed": [], "skipped": list(plan), "spent": 0.0,
                    "reason": "starter already reserved today"}
        if self._events_this_month(state) >= config.FOREVER6_STARTER_MAX_EVENTS_PER_MONTH:
            return {"placed": [], "skipped": list(plan), "spent": 0.0,
                    "reason": "starter monthly cap reached"}

        # Re-verify SETTLED CASH at execution time. F6 is CASH-ONLY — margin is FORBIDDEN (the cold
        # board's decisive ruin finding: a maintenance call from ANY other strategy's bad day would
        # force-liquidate the never-sell book). settled_cash comes from account.cash, never buying_power.
        cash = float(settled_cash) if settled_cash is not None else self._fetch_cash()
        # Hard SEGREGATION guard: never spend into the dry powder the deep crash ladder reserves
        # (ammo-cannibalization ruin finding). spendable is bounded by BOTH the per-event budget and
        # the cash floor — whichever is tighter.
        spendable = min(float(budget), cash - config.FOREVER6_STARTER_CASH_FLOOR)
        if spendable <= 0:
            logger.warning("[F6] execute_starter: no spendable cash (cash $%.0f, floor $%.0f, budget $%.0f) — nothing placed",
                           cash, config.FOREVER6_STARTER_CASH_FLOOR, float(budget))
            return {"placed": [], "skipped": list(plan), "spent": 0.0, "reason": "cash-floor/budget guard"}

        from execution import broker as _bk
        placed: list[dict] = []
        skipped: list[dict] = []
        reserved = 0.0
        event_id: str | None = None

        # Reject malformed or forbidden products before touching market data.
        # The same checks repeat inside each symbol lock because the live asset
        # description or configured universe can change between preflight and
        # the money-moving boundary.
        admissible_plan: list[dict] = []
        for candidate in plan:
            candidate_symbol = str(candidate.get("symbol", "")).upper()
            try:
                candidate_price = float(candidate.get("price", 0) or 0)
            except (TypeError, ValueError):
                candidate_price = 0.0
            if not candidate_symbol or candidate_price <= 0:
                skipped.append({**candidate, "skip": "bad symbol/price"})
                continue
            eligible, policy_reason = f6_symbol_eligible(
                candidate_symbol, config.FOREVER6_UNIVERSE
            )
            if not eligible:
                skipped.append({**candidate, "skip": f"instrument policy: {policy_reason}"})
                logger.critical(
                    "[F6] BUY BLOCKED %s — instrument policy: %s",
                    candidate_symbol, policy_reason,
                )
                _slack_safe(
                    f":no_entry: F6 BUY BLOCKED for {candidate_symbol} — instrument "
                    f"policy: {policy_reason}. No order was sent."
                )
                continue
            try:
                live_asset = self.broker.get_trading_client().get_asset(candidate_symbol)
            except Exception:
                skipped.append({
                    **candidate, "skip": "Alpaca asset classification unavailable",
                })
                continue
            live_ok, live_reason = f6_live_asset_eligible(candidate_symbol, live_asset)
            if not live_ok:
                skipped.append({
                    **candidate, "skip": f"live instrument policy: {live_reason}",
                })
                logger.critical(
                    "[F6] BUY BLOCKED %s — live instrument policy: %s",
                    candidate_symbol, live_reason,
                )
                _slack_safe(
                    f":no_entry: F6 BUY BLOCKED for {candidate_symbol} — live instrument "
                    f"policy: {live_reason}. No order was sent."
                )
                continue
            admissible_plan.append(candidate)
        if not admissible_plan:
            return {
                "placed": [], "submitted": [], "skipped": skipped,
                "spent": 0.0, "reserved": 0.0,
                "reason": "no admissible F6 starter names",
            }

        # Feed-liveness preflight happens before the first irreversible action.
        # A latest-quote REST call commonly returns the same quote when repeated
        # immediately, especially after hours.  Instead, each symbol must advance
        # beyond the timestamp observed on a prior scan cycle.  Observe every
        # planned symbol together so a partially live feed cannot create a partial
        # basket and then strand the remaining names behind daily idempotency.
        observations: dict[str, tuple[float, float, datetime] | None] = {}
        for candidate in admissible_plan:
            candidate_symbol = str(candidate.get("symbol", "")).upper()
            if not candidate_symbol:
                continue
            raw_quote = self.quote_loader(candidate_symbol)
            observed_at = self.clock().astimezone(ET)
            observations[candidate_symbol] = _validated_quote(raw_quote, observed_at)
        confirmed_quotes, blocked_symbols = _observe_advancing_basket(observations)
        if blocked_symbols:
            skipped.extend(
                {
                    **candidate,
                    "skip": (
                        "quote feed freshness awaits a later cross-cycle update"
                        if str(candidate.get("symbol", "")).upper() in blocked_symbols
                        else "basket held until every quote proves freshness"
                    ),
                }
                for candidate in admissible_plan
            )
            return {
                "placed": [], "submitted": [], "skipped": skipped,
                "spent": 0.0, "reserved": 0.0,
                "reason": "quote feed freshness not yet proven across cycles",
            }

        for i, p in enumerate(admissible_plan):
            sym = str(p.get("symbol", "")).upper()
            try:
                px = float(p.get("price", 0) or 0)
            except (TypeError, ValueError):
                px = 0.0
            if not sym or px <= 0:
                skipped.append({**p, "skip": "bad symbol/price"})
                continue
            # Re-check at the money-moving boundary.  A caller can pass an old,
            # hand-built, or corrupted plan that never passed maybe_start_accumulation.
            # No F6 order may rely on the planner as its only product-policy gate.
            eligible, policy_reason = f6_symbol_eligible(sym, config.FOREVER6_UNIVERSE)
            if not eligible:
                skipped.append({**p, "skip": f"instrument policy: {policy_reason}"})
                logger.critical("[F6] BUY BLOCKED %s — instrument policy: %s", sym, policy_reason)
                _slack_safe(
                    f":no_entry: F6 BUY BLOCKED for {sym} — instrument policy: "
                    f"{policy_reason}. No order was sent."
                )
                continue
            # Direct broker classification at the final money boundary.  The
            # synchronous response is consumed under the same symbol-order gate;
            # unreadable or contradictory identity fails closed before either
            # the ownership reservation or broker order exists.
            try:
                # Use the broker module's public singleton accessor.  The
                # production caller injects execution.broker itself, whose
                # get_trading_client() returns the paper=True Alpaca client.
                live_asset = self.broker.get_trading_client().get_asset(sym)
            except Exception as asset_exc:
                skipped.append({**p, "skip": "Alpaca asset classification unavailable"})
                logger.critical("[F6] BUY BLOCKED %s — live asset lookup failed: %s", sym, asset_exc)
                _slack_safe(
                    f":no_entry: F6 BUY BLOCKED for {sym} — Alpaca asset "
                    "classification was unavailable. No order was sent."
                )
                continue
            live_ok, live_reason = f6_live_asset_eligible(sym, live_asset)
            if not live_ok:
                skipped.append({**p, "skip": f"live instrument policy: {live_reason}"})
                logger.critical("[F6] BUY BLOCKED %s — live instrument policy: %s", sym, live_reason)
                _slack_safe(
                    f":no_entry: F6 BUY BLOCKED for {sym} — live instrument "
                    f"policy: {live_reason}. No order was sent."
                )
                continue
            # The final order boundary independently requires the exact armed
            # empirical decision that the planner fsynced this ET session.  A
            # direct caller or stale hand-built plan cannot bypass the trigger.
            if not self._trigger_evidence_is_current(trigger_evidence):
                skipped.append({**p, "skip": "trigger evidence unresolved"})
                logger.critical(
                    "[F6] BUY BLOCKED %s — exact current trigger evidence is unresolved",
                    sym,
                )
                _slack_safe(
                    f":no_entry: F6 BUY BLOCKED for {sym} — exact current trigger "
                    "evidence is unresolved. No order was sent."
                )
                continue
            from execution.tier_capital_allocator import (
                live_admit,
                live_bind,
                live_release,
                live_release_proven_no_order,
            )
            from execution.order_admission import active_reductions, symbol_order_gate

            _coid = _bk._make_idem_id("forever6", sym, "buy")
            _capital = None
            _reservation_written = False
            _event_intent_written = False
            order = None
            hard_limit = 0.0
            try:
                with symbol_order_gate(sym):
                    submit_now = self.clock().astimezone(ET)
                    if not _f6_entry_session(submit_now):
                        skipped.append({**p, "skip": "F6 entry session closed"})
                        break

                    boundary_raw_quote = self.quote_loader(sym)
                    quote_observed_at = self.clock().astimezone(ET)
                    confirmed_quote = _validated_quote(boundary_raw_quote, quote_observed_at)
                    if (confirmed_quote is None
                            or confirmed_quote[2] < confirmed_quotes[sym][2]):
                        raise RuntimeError(
                            "boundary quote is invalid, regressed, crossed, future, or outside this ET session"
                        )
                    _, ask, _ = confirmed_quote
                    hard_limit = _bk.canonical_buy_limit_price(ask)

                    current_cash = self._fetch_cash()
                    current_capacity = min(
                        float(budget),
                        current_cash - config.FOREVER6_STARTER_CASH_FLOOR,
                    ) - reserved
                    if (not math.isfinite(current_cash) or current_cash < 0
                            or not math.isfinite(current_capacity) or hard_limit > current_capacity):
                        skipped.append({**p, "skip": "exact limit exceeds current cash/event capacity"})
                        continue
                    current_state = self._load_state_strict()
                    current_today = datetime.now(PT).strftime("%Y-%m-%d")
                    other_counted = [
                        event for event in current_state.get("events", [])
                        if self._event_counts(event) and event.get("event_id") != event_id
                    ]
                    if any(str(event.get("date", "")) == current_today for event in other_counted):
                        raise RuntimeError("another F6 event is already reserved today")
                    if sum(1 for event in other_counted
                           if str(event.get("date", "")).startswith(current_today[:7])) \
                            >= config.FOREVER6_STARTER_MAX_EVENTS_PER_MONTH:
                        raise RuntimeError("F6 monthly event cap reached")

                    _unsettled_reducers = active_reductions(sym)
                    if _unsettled_reducers:
                        skipped.append({**p, "skip": "reducing order admission unresolved"})
                        continue
                    _open = _bk.get_open_orders(sym, nested=True)
                    if _open is None:
                        raise RuntimeError("open-order state unreadable")
                    _all_open = []
                    for _parent in _open:
                        _all_open.append(_parent)
                        _all_open.extend(list(getattr(_parent, "legs", None) or []))
                    _reducing = []
                    for _o in _all_open:
                        _symbol = str(getattr(_o, "symbol", "") or "").upper()
                        _side = str(getattr(getattr(_o, "side", ""), "value", getattr(_o, "side", ""))).lower()
                        if _symbol != sym or _side not in {"buy", "sell", "sell_short"}:
                            raise RuntimeError("open-order identity is malformed")
                        if _side in {"sell", "sell_short"}:
                            _reducing.append(str(getattr(_o, "id", "unknown")))
                    if _reducing:
                        skipped.append({**p, "skip": "live reducing order exists"})
                        continue

                    _capital = live_admit(
                        "forever6", "forever6", sym, "buy", 1, hard_limit,
                        stop_price=math.nextafter(0.0, 1.0), exact_cash_reserve=True,
                        order_type="limit", time_in_force="day", extended_hours=True,
                        client_order_id=_coid,
                    )
                    if not _capital.approved:
                        skipped.append({**p, "skip": f"allocator: {_capital.reason}"})
                        continue

                    from execution import ownership_guard as _og
                    _ledger = _og.load_ledger()
                    _entry = (_ledger.get("positions") or {}).get(sym)
                    _baseline = _og.tier_qty(_ledger, sym, "forever6")
                    if (not math.isfinite(_baseline) or _baseline < 0
                            or abs(_baseline - round(_baseline)) > _EPS
                            or (_entry is not None and abs(float(_entry.get("drift", 0) or 0)) > _EPS)):
                        raise RuntimeError("baseline F6 ownership is unresolved")

                    event_id = self._reserve_event_intent(
                        event_id, sym, _coid, hard_limit, trigger_evidence
                    )
                    _event_intent_written = True
                    _reservation = _pending_f6.reserve(
                        sym, 1, _coid, _baseline,
                        limit_price=hard_limit, extended_hours=True,
                    )
                    _reservation_written = True
                    order = _bk.submit_limit_order(
                        sym, 1, "buy", hard_limit, extended_hours=True,
                        tier="forever6", client_order_id=_coid,
                        time_in_force="day", distinguish_rejection=True,
                    )
                    if order is _bk.ORDER_DEFINITIVELY_REJECTED:
                        if not live_release_proven_no_order(
                            _capital.lease, "broker_definitive_reject"
                        ):
                            raise RuntimeError("allocator failed to persist definitive-reject release")
                        _pending_f6.release_definitive_reject(_reservation, _coid)
                        self._update_event_intent(event_id, _coid, "rejected", None)
                        skipped.append({**p, "skip": "broker definitively rejected order"})
                        skipped.extend({**q, "skip": "stopped after broker rejection"}
                                       for q in admissible_plan[i + 1:])
                        break
                    if order is None:
                        order = live_release(_capital.lease, "submit_none")
                        if order is None:
                            order = _bk.get_order_by_client_order_id(_coid)
                    if order is not None:
                        _pending_f6.bind(_reservation, order)
                        if not live_bind(_capital.lease, order):
                            raise RuntimeError("allocator could not bind exact broker order")
            except Exception as exc:
                order = None
                if _capital is not None:
                    if not _reservation_written:
                        live_release(_capital.lease, "pre_submit_reservation_failed")
                    else:
                        recovered = live_release(_capital.lease, "submit_none")
                        if recovered is None:
                            recovered = _bk.get_order_by_client_order_id(_coid)
                        if recovered is not None:
                            _pending_f6.bind(_reservation, recovered)
                            live_bind(_capital.lease, recovered)
                            order = recovered
                if order is None:
                    if event_id is not None and _event_intent_written:
                        self._update_event_intent(event_id, _coid, "ambiguous", None)
                    logger.error("[F6] BUY EXCEPTION %s: %s — STOPPING starter", sym, exc)
                    skipped.append({**p, "skip": f"broker boundary unresolved: {exc}"})
                    skipped.extend({**q, "skip": "stopped after broker exception"}
                                   for q in admissible_plan[i + 1:])
                    break
                logger.warning("[F6] BUY EXCEPTION %s recovered exact order %s by client ID",
                               sym, getattr(order, "id", None))

            if order is None:
                if event_id is not None and _event_intent_written:
                    self._update_event_intent(event_id, _coid, "ambiguous", None)
                logger.error("[F6] BUY UNCONFIRMED %s — stopping starter", sym)
                skipped.extend({**q, "skip": "stopped after unconfirmed order"}
                               for q in admissible_plan[i + 1:])
                break
            oid = getattr(order, "id", None)
            status = str(getattr(getattr(order, "status", ""), "value",
                                 getattr(order, "status", ""))).lower()
            submitted = {
                "symbol": sym, "qty": 1, "hard_limit": hard_limit,
                "reserved": hard_limit, "order_id": str(oid) if oid else None,
                "status": status or "submitted", "client_order_id": _coid,
            }
            placed.append(submitted)
            reserved += hard_limit
            self._update_event_intent(
                cast(str, event_id), _coid, "submitted", submitted
            )
            logger.warning("[F6] STARTER BUY submitted: %s 1sh limit $%.2f (order %s, status %s)",
                           sym, hard_limit, oid, status or "unknown")

        if event_id is not None and not placed:
            self._release_empty_event(event_id)
        if placed:
            self._verify_ledger_reflects(placed)
        return {
            "placed": placed, "submitted": placed, "skipped": skipped,
            "spent": 0.0, "reserved": round(reserved, 2), "reason": "ok",
        }

    def _verify_ledger_reflects(self, placed: list[dict]) -> None:
        """C-2 wrapper — STRUCTURALLY enforces 'never raises into execute_starter': on ANY
        unexpected error it fail-CLOSES (sets the persisted degraded flag + alerts), so a
        crash mid-verify can never leave the buy placed-but-unverified with the next-seed
        block silently disarmed (cold-2nd 2026-07-17). The block-at-entry guarantees the
        flag was clear before we placed, so a fail-closed SET here is always correct."""
        try:
            self._verify_ledger_reflects_inner(placed)
        except Exception as _e:
            logger.critical("[F6] post-buy verify: UNEXPECTED error (%s) — fail-closed: "
                            "setting degraded flag + blocking further F6 seeding.", _e)
            _slack_safe(f":rotating_light: F6 post-buy verify crashed ({_e}) — further F6 "
                        f"seeding BLOCKED (fail-closed). Verify anchor(s) before next restart.")
            _write_sync_degraded(True, f"verify exception: {_e}")

    def _verify_ledger_reflects_inner(self, placed: list[dict]) -> None:
        """C-2 (design logs/f6_prereq1_syncgap_design_2026-07-17.md): after an F6 buy, run
        the authoritative full-replay sync (run_ledger_sync.sync_once) and verify EACH
        placed symbol now has forever6 qty >= bought AND abs(drift) <= eps, retrying with
        backoff (a just-placed market fill can lag the fills feed → healed=True with the lot
        still absent is a real, expected outcome, so the ledger must be positively checked).
        Also alerts if the sync planted NEW drift on a previously-clean protected symbol
        (the multi-tier landmine). A placed order that is TERMINALLY rejected/canceled is
        dropped from the wait-set (no anchor to protect → no false 'unprotected' alert). On
        retry-budget exhaustion with a still-pending fill: CRITICAL + Slack + set the
        persisted degraded flag (blocks the next seed, C-3). Never sells; never raises into
        execute_starter. The block-at-entry guarantees the flag was clear before we placed,
        so writing degraded=False on success only ever CONFIRMS clean (no spurious unblock)."""
        import time as _time
        try:
            import run_ledger_sync as _rls
            from execution import broker as _bk
            from execution import ownership_guard as _og
        except Exception as e:
            logger.critical("[F6] post-buy verify: import failed (%s) — setting degraded flag", e)
            _write_sync_degraded(True, f"import failure: {e}")
            return

        want: dict[str, float] = {}
        oid_by_sym: dict[str, str] = {}
        for p in placed:
            s = str(p.get("symbol", "")).upper()
            if not s:
                continue
            want[s] = want.get(s, 0) + int(p.get("qty", 1) or 1)
            if p.get("order_id"):
                oid_by_sym[s] = str(p["order_id"])

        # Snapshot previously-clean protected symbols (floor>0, drift≈0) BEFORE syncing, so a
        # drift this sync PLANTS on one of them (the multi-tier §2 landmine) is surfaced.
        try:
            _before = _og.load_ledger()
            _clean_before = {
                s for s in _before.get("positions", {})
                if _og.protected_floor(_before, s) > _EPS
                and abs(float(_before["positions"][s].get("drift", 0.0) or 0.0)) <= _EPS
            }
        except Exception:
            _clean_before = set()

        _delays = (2.0, 4.0, 8.0)
        for _attempt in range(len(_delays) + 1):
            resting: list[str] = []
            # A valid DAY limit may remain open until the session ends.  Zero-fill
            # open orders stay protected by both durable journals and are not an
            # ownership-sync failure.  Positive fills must be proved in the ledger.
            for s in list(want):
                _oid = oid_by_sym.get(s)
                if not _oid:
                    continue
                try:
                    _order = _bk.get_order(_oid)
                    _st = str(getattr(getattr(_order, "status", ""), "value",
                                      getattr(_order, "status", "")) or "").lower()
                    _filled = float(getattr(_order, "filled_qty", 0) or 0)
                except Exception:
                    _st = ""
                    _filled = math.nan
                if (_st in {"new", "accepted", "pending_new", "partially_filled",
                            "pending_cancel", "pending_replace", "done_for_day"}
                        and math.isfinite(_filled) and _filled == 0):
                    resting.append(s)
                    want.pop(s, None)
                    continue
                if (math.isfinite(_filled) and _filled > 0
                        and _filled <= float(want[s])):
                    want[s] = _filled
                    continue
                if (_st in ("rejected", "canceled", "cancelled", "expired")
                        and math.isfinite(_filled) and _filled == 0):
                    logger.warning("[F6] post-buy verify: %s order %s is %s — no anchor; "
                                   "dropping from wait-set.", s, _oid, _st)
                    want.pop(s, None)
            if not want:
                if resting:
                    logger.warning(
                        "[F6] bounded BUY pending without fill: %s; durable cash/ownership "
                        "reservations remain active.", resting,
                    )
                else:
                    logger.info("[F6] post-buy verify: no filled anchors to verify.")
                return

            _rls.sync_once()
            try:
                led = _og.load_ledger()
            except Exception as e:
                logger.warning("[F6] post-buy verify: ledger unreadable after sync: %s", e)
                led = None

            if led is not None:
                _pos = led.get("positions", {})
                _all_ok = True
                for s, q in want.items():
                    _f6 = _og.tier_qty(led, s, "forever6")
                    _drift = abs(float(_pos.get(s, {}).get("drift", 0.0) or 0.0))
                    if _f6 + _EPS < q or _drift > _EPS:
                        _all_ok = False
                        break
                _new_drift = [s for s in _clean_before
                              if abs(float(_pos.get(s, {}).get("drift", 0.0) or 0.0)) > _EPS]
                if _new_drift:
                    logger.critical("[F6] post-buy sync planted drift on previously-clean "
                                    "protected %s — verify before enabling exits.", _new_drift)
                    _slack_safe(f":rotating_light: F6 post-seed sync planted drift on "
                                f"{_new_drift} — verify before enabling exits.")
                if _all_ok:
                    logger.warning("[F6] post-buy verify OK: %s ledger-reflected "
                                   "(forever6 floor set, drift≈0).", dict(want))
                    _write_sync_degraded(False, "verified")
                    return

            if _attempt < len(_delays):
                _time.sleep(_delays[_attempt])

        logger.critical("[F6] post-buy verify FAILED after retries — anchor(s) %s not "
                        "ledger-reflected (floor dormant anyway; verify before next "
                        "restart). Blocking further F6 seeding.", dict(want))
        _slack_safe(f":rotating_light: F6 anchor(s) {list(want)} NOT ledger-reflected after "
                    f"retries — further F6 seeding BLOCKED until a clean sync. Verify before "
                    f"next restart.")
        _write_sync_degraded(True, f"verify failed for {list(want)}")

    @staticmethod
    def _save_state(state: dict) -> None:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE.with_suffix(f".json.{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(tmp), str(_STATE))
            directory_fd = os.open(_STATE.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _reserve_event_intent(
        self,
        event_id: str | None,
        symbol: str,
        client_order_id: str,
        hard_limit: float,
        trigger_evidence: dict | None,
    ) -> str:
        """Persist same-day idempotency before the broker can accept an order."""
        with _state_locked():
            state = self._load_state_strict()
            events = state.get("events", [])
            if not isinstance(events, list):
                raise RuntimeError("F6 event state is malformed")
            now_pt = datetime.now(PT)
            today = now_pt.strftime("%Y-%m-%d")
            month = today[:7]
            other_counted = [
                row for row in events
                if self._event_counts(row) and row.get("event_id") != event_id
            ]
            if any(str(row.get("date", "")) == today for row in other_counted):
                raise RuntimeError("another F6 event is already reserved today")
            if (sum(1 for row in other_counted
                    if str(row.get("date", "")).startswith(month))
                    >= config.FOREVER6_STARTER_MAX_EVENTS_PER_MONTH):
                raise RuntimeError("F6 monthly event cap reached")
            if event_id is None:
                event_id = str(uuid.uuid4())
                events.append({
                    "event_id": event_id,
                    "date": today,
                    "ts": now_pt.isoformat(),
                    "status": "reserved",
                    "intents": [],
                    "submitted": [],
                    "spent": 0.0,
                    "trigger_evidence": trigger_evidence,
                })
            event = next((row for row in events if row.get("event_id") == event_id), None)
            if not isinstance(event, dict) or event.get("status") == "released_no_order":
                raise RuntimeError("F6 event reservation is missing")
            intents = event.setdefault("intents", [])
            if any(row.get("client_order_id") == client_order_id for row in intents):
                raise RuntimeError("F6 event intent client ID was already used")
            intents.append({
                "symbol": symbol,
                "qty": 1,
                "client_order_id": client_order_id,
                "hard_limit": round(float(hard_limit), 2),
                "status": "reserved",
            })
            state["events"] = events
            self._save_state(state)
            return event_id

    def _update_event_intent(
        self, event_id: str, client_order_id: str, status: str, order: dict | None,
    ) -> None:
        with _state_locked():
            state = self._load_state_strict()
            events = state.get("events", [])
            event = next((row for row in events if row.get("event_id") == event_id), None)
            if not isinstance(event, dict):
                raise RuntimeError("F6 event reservation disappeared")
            intent = next((row for row in event.get("intents", [])
                           if row.get("client_order_id") == client_order_id), None)
            if not isinstance(intent, dict):
                raise RuntimeError("F6 event intent disappeared")
            intent["status"] = status
            if order is not None:
                event.setdefault("submitted", []).append(order)
            statuses = {str(row.get("status", "")) for row in event.get("intents", [])}
            event["status"] = "ambiguous" if "ambiguous" in statuses else (
                "submitted" if "submitted" in statuses else "reserved"
            )
            self._save_state(state)

    def _release_empty_event(self, event_id: str) -> None:
        with _state_locked():
            state = self._load_state_strict()
            event = next((row for row in state.get("events", [])
                          if row.get("event_id") == event_id), None)
            if not isinstance(event, dict):
                raise RuntimeError("F6 event reservation disappeared")
            statuses = {str(row.get("status", "")) for row in event.get("intents", [])}
            if statuses and statuses <= {"rejected"}:
                event["status"] = "released_no_order"
                event["released_at"] = datetime.now(PT).isoformat()
                self._save_state(state)

    # ── read helpers (1a) ──────────────────────────────────────────────────────
    def _fetch_cash(self) -> float:
        try:
            acct = self.broker.get_account()
            return float(getattr(acct, "cash", 0) or 0)
        except Exception as e:
            logger.warning("forever6: cash fetch failed: %s", e)
            return 0.0

    def _fetch_prices(self) -> dict[str, float]:
        out: dict[str, float] = {}
        try:
            from data.alpaca_data import get_latest_trade
        except Exception:
            return out
        for sym in self.universe:
            try:
                p = get_latest_trade(sym)
                if p and p > 0:
                    out[sym] = float(p)
            except Exception as e:
                logger.debug("forever6: price fetch %s failed: %s", sym, e)
        return out

    # ── EXIT: trims only (F6 exit increment, 2026-08-05) ───────────────────────
    def evaluate_and_execute_trims(self) -> dict:
        """FOREVER-6 EXIT: trim FOREVER6_TRIM_1_FRAC (25%) of current forever6 holdings at
        FOREVER6_TRIM_1_MULT (10x) unrealized gain, another FOREVER6_TRIM_2_FRAC (25%) of
        what remains at FOREVER6_TRIM_2_MULT (20x). NEVER a full sell — the house-money core
        keeps shrinking, never disappears. No stop, no max-hold, no other exit exists for
        this tier (logs/forever6_integration_map_2026-07-09.md §6).

        Sequential within one pass: the 10x trim (if newly triggered) executes and its
        result feeds the 20x check in the SAME call, so a single overnight gap past both
        thresholds still fires both rungs in order, each against the correctly-shrunk qty —
        never a double-sized trim from evaluating both against the pre-trim qty.

        RESERVE-THEN-CONFIRM per rung (GAI preship catch, 2026-08-05): each rung's state is
        "none" → "reserved" → "done". The "reserved" write is durable-confirmed BEFORE the
        sell is even attempted — so a crash/restart between reservation and sell always
        finds "reserved" and refuses to re-fire (fail closed), never "none" again. A sell
        that fails after reservation ALSO stays "reserved" forever (never reverts to
        "none") — this makes a lost/failed _save_trim_state or a failed sell both degrade
        to "block and alert," never to "silently retry and over-trim." Clearing a stuck
        "reserved" back to firing requires a human to edit forever6_trims.json after
        verifying against Alpaca's own order history — there is no auto-retry path.

        A trim that rounds to 0 shares (25% of a 1-2 share position) is SKIPPED, not
        forced up to a full-position sell — trimming a fraction must never become
        liquidating the whole position; the position is re-evaluated next cycle in case a
        deeper crash-ladder rung later adds enough shares to make a fractional trim
        possible. Per-symbol failure is isolated — one symbol's read/compute error never
        blocks evaluation of the others. Returns {symbol: {"gain_mult": float, "fired":
        [(label, qty), ...]}} for symbols where a trim executed this pass.

        KNOWN PRE-LIVE-FLIP GATE (cold-2nd fresh-review, 2026-08-05, fails safe — not
        blocking this ship since FOREVER6_ENABLED stays False): a same-pass 10x-then-20x
        double trim calls the guard twice in quick succession, and the guard's drift-freeze
        check (ownership_guard.check_never_sell_floor) compares the ownership ledger's
        tier-sum (refreshed only by the standalone run_ledger_sync cron, not resynced
        mid-pass here) against Alpaca's live net qty. If trim1's fill reaches Alpaca before
        the ledger resyncs, trim2's guard call can see a transient mismatch and REJECT —
        which this code correctly treats as a sell failure (rung stuck 'reserved',
        alerting, blocking) rather than an over-trim, but means a same-pass double trim may
        not reliably fire both rungs in practice. Fix before flipping FOREVER6_ENABLED:
        force a ledger resync between the two rungs, mirroring the buy-side's
        _verify_ledger_reflects pattern.
        """
        results: dict[str, dict] = {}
        if (config.FOREVER6_ENABLED and not _STARTUP_READY) or _read_sync_degraded():
            logger.critical(
                "[F6] trim evaluation BLOCKED — ownership sync is degraded; "
                "a discretionary trim cannot run against ambiguous tier quantity."
            )
            return results
        try:
            from execution import ownership_guard as _og
        except Exception as e:
            logger.error("[F6] evaluate_and_execute_trims: ownership_guard import failed "
                        "— skipping this cycle: %s", e)
            return results
        try:
            ledger = _og.load_ledger()
        except Exception as e:
            logger.warning("[F6] evaluate_and_execute_trims: ledger unreadable — "
                           "skipping this cycle: %s", e)
            return results

        # Fail the WHOLE pass closed on a corrupt (not merely absent) trim-state file —
        # checked once, up front, so a corruption doesn't silently read as "no trim has
        # ever happened" for every symbol (see _TrimStateUnreadable's docstring).
        try:
            self._load_trim_state()
        except _TrimStateUnreadable as e:
            logger.critical("[F6] evaluate_and_execute_trims: trim state file is "
                            "corrupt/unreadable — refusing ALL trim evaluation this "
                            "cycle (fail closed) until it's manually repaired: %s", e)
            _slack_safe(f":rotating_light: F6 forever6_trims.json is corrupt/unreadable "
                        f"— ALL trims blocked until manually repaired: {e}")
            return results

        prices = self._fetch_prices()

        for sym in self.universe:
            try:
                own = _og.tier_qty(ledger, sym, "forever6")
                if own <= _EPS:
                    continue
                entry = ledger.get("positions", {}).get(sym, {})
                avg_cost = float(
                    entry.get("tiers", {}).get("forever6", {}).get("avg_cost", 0.0) or 0.0
                )
                if avg_cost <= 0:
                    logger.debug("[F6] %s: forever6 qty=%s but avg_cost<=0 — skipping trim "
                                "eval this cycle.", sym, own)
                    continue
                px = prices.get(sym)
                if not px or px <= 0:
                    continue
                gain_mult = px / avg_cost
                current_qty = own
                fired: list[tuple[str, int]] = []

                if gain_mult >= config.FOREVER6_TRIM_1_MULT:
                    qty1 = int(current_qty * config.FOREVER6_TRIM_1_FRAC)
                    try:
                        ok1, sold1 = self._try_rung(sym, "trim1", qty1, "10x", gain_mult)
                    except _TrimStateUnreadable as e:
                        # Handled immediately, right at this call site (not via an
                        # outer except clause several lines away) — corruption
                        # DETECTED mid-loop must abort the WHOLE pass with the same
                        # loud alert as the upfront check, never degrade into a
                        # per-symbol warning + continue (cold-2nd + CI audit catch,
                        # 2026-08-05).
                        self._alert_corrupt_trim_state(sym, e)
                        return results
                    if ok1:
                        current_qty -= sold1
                        fired.append(("10x", sold1))

                if gain_mult >= config.FOREVER6_TRIM_2_MULT:
                    try:
                        _trim1_status = (
                            self._load_trim_state().get(sym, {}).get("trim1", "none")
                        )
                    except _TrimStateUnreadable as e:
                        self._alert_corrupt_trim_state(sym, e)
                        return results
                    if _trim1_status != "done":
                        # 20x must never fire ahead of (or instead of) 10x — "another 25%
                        # of what remains" presupposes the first trim already happened.
                        # Covers: trim1 not yet attempted, trim1's qty rounded to 0 (too
                        # small to trim), and trim1 stuck "reserved" after a failed sell.
                        logger.debug(
                            "[F6] %s: 20x trim trigger met (gain %.1fx) but trim1 status "
                            "is %r, not 'done' — skipping 20x until trim1 completes.",
                            sym, gain_mult, _trim1_status)
                    else:
                        qty2 = int(current_qty * config.FOREVER6_TRIM_2_FRAC)
                        try:
                            ok2, sold2 = self._try_rung(sym, "trim2", qty2, "20x", gain_mult)
                        except _TrimStateUnreadable as e:
                            self._alert_corrupt_trim_state(sym, e)
                            return results
                        if ok2:
                            fired.append(("20x", sold2))

                if fired:
                    results[sym] = {"gain_mult": round(gain_mult, 2), "fired": fired}
                    _slack_safe(f":moneybag: F6 TRIM {sym}: gain {gain_mult:.1f}x — {fired}")
            except Exception as e:
                logger.warning("[F6] evaluate_and_execute_trims: %s failed, skipping: %s",
                               sym, e)
                continue

        return results

    def _alert_corrupt_trim_state(self, sym: str, e: Exception) -> None:
        """CRITICAL log + Slack alert for a _TrimStateUnreadable detected mid-cycle.
        Void helper — every call site is responsible for its own `return results`
        immediately after calling this, so the abort is visible right where it
        happens rather than hidden behind a shared return path."""
        logger.critical(
            "[F6] evaluate_and_execute_trims: trim state file became corrupt/unreadable "
            "mid-cycle (detected while evaluating %s) — refusing further trim "
            "evaluation this cycle (fail closed) until it's manually repaired: %s",
            sym, e)
        _slack_safe(
            f":rotating_light: F6 forever6_trims.json became corrupt/unreadable "
            f"mid-cycle — remaining trims this cycle BLOCKED until manually repaired: "
            f"{e}")

    def _try_rung(self, symbol: str, rung: str, qty: int, label: str,
                  gain_mult: float) -> tuple[bool, int]:
        """Attempt ONE trim rung ("trim1" or "trim2") with reserve-then-confirm state.
        Returns (fired: bool, qty_sold: int). Never re-fires a rung whose state is
        anything other than "none" — "reserved" (whether from a persist race or an actual
        sell failure) and "done" both permanently block further auto-attempts for this
        rung on this symbol until a human clears the state file."""
        state = self._load_trim_state()
        sym_state = dict(state.get(symbol, {}))
        status = sym_state.get(rung, "none")

        if status not in ("none", "reserved", "done"):
            # Fail CLOSED on an unrecognized value (corruption, or a typo during the
            # documented manual-clear procedure) — never fall through and treat an
            # unknown status the same as "none" (cold-2nd fresh-review catch, 2026-08-05).
            logger.critical(
                "[F6] %s %s rung has an UNRECOGNIZED state %r — treating as blocked, "
                "NOT as 'none'. Manually correct forever6_trims.json to one of "
                "none/reserved/done.", symbol, label, status)
            _slack_safe(f":rotating_light: F6 {symbol} {label} trim state is "
                        f"unrecognized ({status!r}) — blocked until manually corrected.")
            return False, 0
        if status == "done":
            return False, 0
        if status == "reserved":
            logger.critical(
                "[F6] %s %s rung is STUCK at 'reserved' (gain %.1fx) — a prior attempt "
                "either failed to persist or the sell itself failed. NOT auto-retrying. "
                "Verify against Alpaca's order history, then manually clear "
                "forever6_trims.json before this rung can fire.", symbol, label, gain_mult)
            _slack_safe(f":rotating_light: F6 {symbol} {label} trim STUCK at 'reserved' "
                        f"— manual verification required before it can fire.")
            return False, 0
        if qty < 1:
            logger.info(
                "[F6] %s: %s trim trigger met (gain %.1fx) but the computed qty rounds "
                "to 0 — skipping (position too small to fractionally trim).",
                symbol, label, gain_mult)
            return False, 0

        # Reserve FIRST, durably, before any sell is attempted. A write failure here
        # means we never sell this cycle — fail closed, never sell without a prior
        # confirmed reservation.
        sym_state[rung] = "reserved"
        state[symbol] = sym_state
        if not self._save_trim_state(state):
            logger.error(
                "[F6] %s: %s trim reservation FAILED to persist — refusing to sell this "
                "cycle (fail closed). Will retry reservation next cycle.", symbol, label)
            return False, 0

        ok = self._submit_trim(symbol, qty, label)

        # Whether the sell succeeded or failed, the rung stays "reserved" — success will
        # be upgraded to "done" below; failure deliberately does NOT revert to "none"
        # (never assume a failed-looking order didn't partially execute).
        if ok:
            # Re-load fresh rather than reuse the pre-sell snapshot (cold-2nd fresh-review
            # catch, 2026-08-05): _submit_trim is a blocking network call, and writing back
            # a stale in-memory `state` here could silently clobber a concurrent manual
            # edit to a DIFFERENT symbol/rung made during that window — exactly the kind of
            # edit this design's own alerts tell an operator to make.
            state = self._load_trim_state()
            sym_state = dict(state.get(symbol, {}))
            sym_state[rung] = "done"
            state[symbol] = sym_state
            if not self._save_trim_state(state):
                logger.critical(
                    "[F6] %s: %s trim EXECUTED (sold %s share(s)) but the 'done' state "
                    "write FAILED — rung stays 'reserved', so it will correctly block "
                    "(not re-fire) next cycle, but needs manual confirmation it "
                    "actually completed: %s", symbol, label, qty, symbol)
                _slack_safe(f":rotating_light: F6 {symbol} {label} trim executed but "
                            f"state write failed — verify and clear manually.")
            return True, qty

        logger.error(
            "[F6] %s: %s trim sell FAILED after reservation — rung stays 'reserved' "
            "(blocks future auto-attempts; verify against Alpaca before manual clear).",
            symbol, label)
        _slack_safe(f":rotating_light: F6 {symbol} {label} trim sell FAILED after "
                    f"reservation — verify against Alpaca's order history, then "
                    f"manually clear forever6_trims.json before retrying.")
        return False, 0

    def _submit_trim(self, symbol: str, qty: int, label: str) -> bool:
        """Execute one trim leg via the sole authorized broker path. Never raises."""
        try:
            from execution import broker as _bk
            ok = _bk.submit_f6_trim(symbol, qty)
        except Exception as e:
            logger.error("[F6] TRIM EXCEPTION %s (%s trigger): %s", symbol, label, e)
            return False
        if ok:
            logger.warning("[F6] TRIM EXECUTED %s: sold %s share(s) (%s trigger)",
                           symbol, qty, label)
        else:
            logger.error("[F6] TRIM FAILED %s: broker rejected/failed the %s trim "
                        "sell of %s share(s)", symbol, label, qty)
        return ok

    def _load_trim_state(self) -> dict:
        """Returns {} ONLY when the file is genuinely absent (a legitimate fresh state —
        no trim has ever been recorded). A PRESENT-but-unreadable/corrupt file raises
        _TrimStateUnreadable instead of silently returning {} (GAI preship catch,
        2026-08-05): treating corruption the same as "fresh" would let a successful
        reserve-write for ONE symbol overwrite every OTHER symbol's already-recorded
        "reserved"/"done" state with a blank slate, reopening the exact re-fire risk this
        whole mechanism exists to close. Callers must let this propagate and skip
        evaluation entirely rather than catch-and-treat-as-empty."""
        if not _TRIM_STATE.exists():
            return {}
        try:
            d = json.loads(_TRIM_STATE.read_text())
        except Exception as e:
            raise _TrimStateUnreadable(f"forever6_trims.json unreadable: {e}") from e
        if not isinstance(d, dict):
            raise _TrimStateUnreadable(f"forever6_trims.json is not a dict: {type(d).__name__}")
        return d

    def _save_trim_state(self, state: dict) -> bool:
        """Atomic tmp→fsync→replace (RC-5). Returns True on confirmed success, False on
        failure — callers MUST check this return value: the reserve-then-confirm design
        depends on the caller refusing to proceed (to a sell, or to marking "done") when
        this returns False, not on this function raising."""
        try:
            _TRIM_STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _TRIM_STATE.with_suffix(f".json.{os.getpid()}.tmp")
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(_TRIM_STATE))
            return True
        except Exception as e:
            logger.critical("[F6] trim state persist FAILED: %s", e)
            _slack_safe(f":rotating_light: F6 trim state persist FAILED ({e}) — verify "
                        f"forever6_trims.json manually before the next after-close cycle.")
            return False
