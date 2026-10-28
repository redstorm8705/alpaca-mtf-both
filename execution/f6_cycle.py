"""Shared Forever-6 routing used by production and the launch simulation.

This module owns orchestration only.  F6 policy, sizing, admission, ownership,
and broker behavior remain in their existing production modules.

Signed: OpenAI Codex, 2026-10-23.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class F6KillRoute:
    block_starter: bool
    reason: str


def resolve_f6_kill_route(
    *, enabled: bool, kill_tripped: bool, control_fault: str, risk: Any
) -> F6KillRoute:
    """Preserve the existing typed-verdict F6 exception to the daily brake."""
    if not enabled:
        return F6KillRoute(True, "f6_disabled")
    if control_fault:
        return F6KillRoute(True, "control_fault")
    if not kill_tripped:
        return F6KillRoute(False, "no_kill")
    try:
        allowed = bool(risk.f6_genuine_loss_bypass_allowed())
    except Exception as exc:  # fail closed; caller keeps every exit path alive
        logger.error(
            "F6 kill-verdict validation failed (%r) — starter remains blocked.", exc
        )
        return F6KillRoute(True, "kill_verdict_unreadable")
    reason = "genuine_trading_loss" if allowed else "kill_not_eligible"
    return F6KillRoute(not allowed, reason)


def run_f6_after_close(
    *,
    enabled: bool,
    is_after_close: bool,
    now_et: datetime,
    block_starter: bool,
    fetch_bars: Callable[..., Any],
    manager: Any | None = None,
) -> dict[str, Any]:
    """Run the existing after-close F6 sequence and return auditable facts."""
    result: dict[str, Any] = {
        "enabled": bool(enabled),
        "after_close": bool(is_after_close),
        "trims_ran": False,
        "starter_considered": False,
        "submitted": [],
        "reason": "inactive",
    }
    if not enabled or not is_after_close:
        return result

    logger.info(
        "change=f6-sim-1023 after-close shared route active block_starter=%s",
        bool(block_starter),
    )

    from execution import broker as f6_broker
    from execution.forever_hold_manager import ForeverHoldManager, _f6_entry_session

    if manager is None:
        manager = ForeverHoldManager(f6_broker)
    try:
        result["trim_result"] = manager.evaluate_and_execute_trims()
        result["trims_ran"] = True
    except Exception as exc:
        logger.warning("F6 trim evaluation failed (non-fatal): %s", exc)
        result["trim_error"] = repr(exc)

    if not _f6_entry_session(now_et):
        result["reason"] = "outside_entry_session"
        return result
    if block_starter:
        result["reason"] = "starter_blocked"
        return result

    result["starter_considered"] = True
    frame = fetch_bars(
        "SPY", config.TF_DAILY, num_bars=config.BARS_TO_FETCH[config.TF_DAILY]
    )
    if frame is None or frame.empty:
        result["reason"] = "spy_bars_unavailable"
        return result
    plan = manager.maybe_start_accumulation(frame, now_et.date())
    if not plan.get("plan"):
        result["reason"] = str(plan.get("reason") or "no_plan")
        return result
    execution = manager.execute_starter(
        plan["plan"],
        plan["budget"],
        trigger_evidence=plan.get("trigger_evidence"),
    )
    submitted = list(execution.get("submitted", []))
    result.update(
        {
            "submitted": submitted,
            "reason": str(execution.get("reason") or "executed"),
            "reserved": float(execution.get("reserved", 0.0) or 0.0),
        }
    )
    logger.warning(
        "[F6] starter executed: submitted %s "
        "(reserved $%.2f; actual spend comes from fills)",
        [row.get("symbol") for row in submitted],
        result["reserved"],
    )
    return result
