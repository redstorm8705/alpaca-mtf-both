# ruff: noqa: E501
"""
execution/risk_manager.py
Position sizing, daily loss limits, and kill switch logic.
The most important file in the bot — protects capital above everything else.
"""

import json
import logging
import os
import requests  # type: ignore[import-untyped]
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import config

_ET = ZoneInfo("America/New_York")
_PT = ZoneInfo("America/Los_Angeles")

logger = logging.getLogger(__name__)

# Alpaca REST base URL — PAPER endpoint. The bot is paper=True (Architecture Invariant #8,
# LOCKED until a full board vote at live launch). Going live flips broker.py's paper flag AND
# this constant together in that one board-gated change — this is the single documented migration
# point for REST calls added here. (Older helpers in this module still inline the paper URL; they
# fold onto this constant over time.)
_ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

# ── BUG-ADV-1: Kill switch state persistence ─────────────────────────────────
# Survives os.execv() watchdog restarts — new RiskManager instances restore
# killed=True from disk if the date matches the current session.
_KILL_STATE_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "state" / "kill_switch_state.json"
)


def _load_kill_state() -> dict:
    """Load persisted kill switch state.

    ABSENT file → {} (never killed / cleared — legitimate un-killed state).
    PRESENT-but-CORRUPT file → FAIL CLOSED (B6 hardening 2026-07-03, board+
    Gro+GAI; same doctrine as the QHM HOLE-1 fix): the old behavior returned
    {} on parse error with a debug-level whisper, silently UN-KILLING the bot
    if the state file was ever corrupted. A risk control must halt loudly on
    unreadable state, not resume trading. Returns a synthetic same-day
    killed=True record + CRITICAL log + Slack.

    FileNotFoundError between exists() and read_text() (GAI audit catch: the
    file can legitimately vanish in that window, e.g. reset/cleanup in another
    process) → treated as ABSENT, never a false kill.

    Recovery from fail-closed: the operator MUST remove/repair the corrupt
    file, then restart (or wait for the next day's reset after removal). While
    the corrupt file remains on disk, every load re-fails closed by design.
    """
    try:
        if _KILL_STATE_FILE.exists():
            try:
                return json.loads(_KILL_STATE_FILE.read_text())
            except FileNotFoundError:
                # exists() → deleted-before-read race — genuinely absent.
                logger.debug("kill state file vanished during load — treating as absent")
                return {}
        return {}
    except Exception as _e:
        logger.critical(
            "KILL-SWITCH STATE FILE UNREADABLE/CORRUPT (%s) — FAILING CLOSED: "
            "treating as killed=True for today. Remove/repair %s, then restart "
            "to clear.",
            _e, _KILL_STATE_FILE,
        )
        try:
            from alerts import send_slack
            send_slack(
                "🚨 KILL-SWITCH state file CORRUPT — failing CLOSED (no new "
                "entries today). Remove/repair data/state/kill_switch_state.json, "
                "then restart to clear."
            )
        except Exception:
            logger.warning("corrupt-kill-state Slack alert failed")
        return {
            "date": datetime.now(_ET).strftime("%Y-%m-%d"),
            "killed": True,
            "halt_entries": True,
            "corrupt_state_fail_closed": True,
        }


def _save_kill_state(killed: bool, halt_entries: bool = False) -> None:
    """
    Persist kill switch + halt state atomically.
    Never raises — cannot break the bot.
    """
    try:
        _KILL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(_KILL_STATE_FILE) + ".tmp")
        tmp.write_text(json.dumps({
            # ET date matches restoration check (Finding 1)
            "date":         datetime.now(_ET).strftime("%Y-%m-%d"),
            "killed":       killed,
            "halt_entries": halt_entries,
            # PT per CLAUDE.md Rule 8 (Finding 9)
            "triggered_at": datetime.now(_PT).isoformat(),
        }))
        tmp.replace(_KILL_STATE_FILE)
    except Exception as _e:
        try:
            # B6 hardening: failing to persist a FIRED kill switch means the
            # kill dies on the next restart — that is a CRITICAL event, not a
            # debug whisper (old behavior). Still never raises.
            logger.critical(
                "KILL-SWITCH STATE PERSIST FAILED (%s) — a fired kill switch "
                "will NOT survive a restart. Investigate disk/state dir now.",
                _e,
            )
            from alerts import send_slack
            send_slack(
                "🚨 Kill-switch persist FAILED — a fired kill will not survive "
                "restart. Check data/state/ write permissions/disk."
            )
        except Exception:
            pass  # logger/alerts broken; persistence failure must not break the trading loop


def _safe_lev_mult(value: Any, config_name: str, symbol: str) -> float:
    """AWP audit fix (2026-06-28): defense-in-depth guard, same bug class as
    the h2_scalar zero-width-stop fix above. The four leveraged-ETF
    multiplier config constants (LEVERAGED_3X_STOP_MULTIPLIER,
    LEVERAGED_3X_TARGET_MULTIPLIER, LEVERAGED_STOP_MULTIPLIER,
    LEVERAGED_TARGET_MULTIPLIER) were multiplied directly into
    stop_mult/target_mult with zero validation — a future typo or bad edit
    setting one to 0 or negative would zero out (or invert) stop/target
    distance entirely. Current values are valid; this guards the future.
    Returns 1.0 (no leverage adjustment, fail-safe) and logs an error if
    the value is not a positive number.
    """
    try:
        fval = float(value)
    except (TypeError, ValueError):
        fval = None
    if fval is None or fval <= 0:
        logger.error(
            "[%s] Invalid %s=%r (must be > 0) — skipping leverage adjustment "
            "(using 1.0x) to avoid a zero-width stop/target.",
            symbol, config_name, value,
        )
        return 1.0
    return fval


class RiskManager:
    def __init__(
        self, portfolio_value: float, daily_start_value: Optional[float] = None
    ):
        self.portfolio_value    = portfolio_value
        # Use Alpaca's last_equity (SOD baseline) when provided so that
        # daily_pnl = equity - last_equity matches the dashboard on restarts.
        # Falls back to portfolio_value (legacy behaviour) if not supplied.
        self.daily_start_value  = (
            daily_start_value if daily_start_value is not None else portfolio_value
        )
        self.open_positions     = 0
        self.daily_pnl          = portfolio_value - self.daily_start_value
        self.killed             = False
        # BUG-ADV-1: restore killed=True if same calendar day (survives os.execv)
        _ks = _load_kill_state()
        _today_et = datetime.now(_ET).strftime("%Y-%m-%d")
        if _ks.get("date") == _today_et and _ks.get("killed"):
            self.killed = True
            logger.warning(
                "Kill switch restored from disk — killed=True persists from prior"
                " watchdog restart. Manual daily_reset() required to clear."
            )

    def update_portfolio_value(self, value: float):
        # Only update portfolio_value — do NOT write daily_pnl here.
        # daily_pnl is owned exclusively by register_close() accumulation.
        # equity_pnl (read-only property below) provides the equity-delta view
        # for the kill switch without overwriting realized P&L.
        self.portfolio_value = value

    @property
    def equity_pnl(self) -> float:
        """Unrealized equity delta: current equity minus SOD baseline."""
        return self.portfolio_value - self.daily_start_value

    def _qhm_unrealized_pl(self) -> float:
        """QHM positions' unrealized P&L from Alpaca POSITION OBJECTS (authoritative,
        no fill-matching). Subtracted from equity_pnl in the kill switch so a
        quarterly-hold drawdown does not trip the INTRADAY kill. Fail-safe: returns
        0.0 on ANY error/absence → kill switch degrades to account-level equity_pnl
        (the conservative direction — never masks a real loss)."""
        try:
            from execution.quarterly_hold_manager import get_quarterly_hold_symbols
            qhm = set(get_quarterly_hold_symbols() or [])
            if not qhm:
                return 0.0
            api_key = os.getenv("ALPACA_API_KEY", "")
            secret = os.getenv("ALPACA_SECRET_KEY", "")
            if not api_key or not secret:
                logger.warning(
                    "kill-switch QHM unrealized: API keys not set — using 0 "
                    "(kill degrades to account-level equity, conservative)"
                )
                return 0.0
            resp = requests.get(
                "https://paper-api.alpaca.markets/v2/positions",
                headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.warning(
                    "kill-switch QHM unrealized: positions HTTP %d — using 0 "
                    "(kill degrades to account-level equity, conservative)",
                    resp.status_code,
                )
                return 0.0
            positions = resp.json()
            if not isinstance(positions, list):
                return 0.0
            total = 0.0
            for p in positions:
                if p.get("symbol") in qhm:
                    try:
                        total += float(p.get("unrealized_pl", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        pass
            return round(total, 2)
        except Exception as e:  # RC-3
            logger.warning("kill-switch QHM unrealized fetch failed (%s) — using 0", e)
            return 0.0

    def check_kill_switch(self) -> bool:
        """Returns True if daily loss limit has been breached. Halts all new trades.
        Measure is Alpaca-EQUITY based (phantom-proof) minus QHM unrealized, OR-guarded
        with raw account equity — see the inline note below. daily_pnl is NOT used in
        the kill decision (it false-tripped at -73.86% on 2026-07-07 via phantom fills)."""
        if self.killed:
            return True
        if self.daily_start_value <= 0:
            logger.critical(
                "KILL SWITCH GUARD: daily_start_value <= 0 — SOD baseline is invalid. "
                "Treating as KILL SWITCH TRIGGERED to prevent unguarded trading. "
                "Manual reset or bot restart required."
            )
            self.killed = True
            # B6 hardening: this kill previously did NOT persist — it silently
            # died on the next restart while the drawdown kill (below) survived.
            _save_kill_state(killed=True, halt_entries=True)
            return True
        # PHANTOM-PROOF kill measure (2026-07-10, board + Gro + GAI). On 2026-07-07
        # the kill switch FALSELY tripped at -73.86% because self.daily_pnl (the
        # register_close accumulation) had absorbed phantom-fill losses, and
        # update_daily_pnl_from_alpaca()'s "keep the bigger loss" guard preserved
        # them. daily_pnl is NO LONGER consulted in the kill DECISION.
        #
        # The measure is Alpaca EQUITY delta (equity = real cash + real market value;
        # structurally immune to any fill-matching bug) minus QHM's UNREALIZED P&L
        # (from Alpaca position objects — authoritative, no fill-matching), so a
        # quarterly-hold drawdown does not trip the INTRADAY kill (board 2026-07-01).
        #
        # intraday_equity_pnl = equity_pnl - qhm_unrealized IS the true intraday P&L:
        #  - QHM up  → equity_pnl is inflated by the QHM gain; subtracting it EXPOSES the
        #    full intraday loss (more negative) so a masked intraday loss still trips.
        #  - QHM down→ subtracting a negative ADDS it back, so a QHM loss does NOT trip
        #    the intraday kill — exactly the exclusion the board wanted.
        # Fail-safe: on any QHM-fetch failure _qhm_unrealized_pl()=0.0 → measure reverts
        # to account-level equity_pnl (MORE sensitive / conservative — never masks a loss).
        # A real 7% intraday loss always trips. daily_pnl remains for display only.
        intraday_equity_pnl = self.equity_pnl - self._qhm_unrealized_pl()
        loss_pct = intraday_equity_pnl / self.daily_start_value
        if loss_pct <= -config.MAX_DAILY_LOSS_PCT:
            logger.critical(
                f"KILL SWITCH TRIGGERED: intraday loss (excl QHM) {loss_pct:.2%} "
                f"exceeds limit {config.MAX_DAILY_LOSS_PCT:.2%} "
                f"(equity_pnl ${self.equity_pnl:+.2f}). Halting new entries."
            )
            self.killed = True
            # Finding 4: killed always implies halt_entries
            _save_kill_state(killed=True, halt_entries=True)
        return self.killed

    def can_open_position(self) -> bool:
        """Returns True if we're allowed to open another position."""
        if self.check_kill_switch():
            return False
        if self.open_positions >= config.MAX_OPEN_POSITIONS:
            logger.info(
                "Max positions (%s) reached. Skipping.", config.MAX_OPEN_POSITIONS
            )
            return False
        return True

    def check_buying_power_for_order(self, shares: int, entry_price: float) -> bool:
        """Live Alpaca buying-power pre-flight before submitting an order (2026-07-14,
        board + Gro + GAI). The bot previously never checked BP — it sized a position and
        fired the order, relying on Alpaca to reject when short, which then drifted the
        tracker vs risk count. This closes that latent over-commit / desync bug.

        Requires remaining buying_power to cover the order notional + a 10% cushion
        (slippage / rounding). FAIL-CLOSED: returns False on ANY error or absence — the
        bot must NEVER over-commit; a missed entry is cheap, an unfunded fill is not.
        """
        try:
            notional = float(shares) * float(entry_price)
            if notional <= 0:
                return False
            api_key = os.getenv("ALPACA_API_KEY", "")
            secret  = os.getenv("ALPACA_SECRET_KEY", "")
            if not api_key or not secret:
                logger.warning("BP pre-flight: API keys not set — failing closed (no entry).")
                return False
            resp = requests.get(
                f"{_ALPACA_BASE_URL}/v2/account",
                headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.warning(
                    "BP pre-flight: account HTTP %d — failing closed (no entry).",
                    resp.status_code,
                )
                return False
            bp = float(resp.json().get("buying_power", 0) or 0)
            required = notional * 1.10
            if bp < required:
                logger.warning(
                    "BP pre-flight FAIL: buying_power $%.2f < required $%.2f "
                    "(notional $%.2f + 10%% cushion) — blocking entry.",
                    bp, required, notional,
                )
                return False
            return True
        except Exception as e:  # RC-3
            logger.warning("BP pre-flight error (%s) — failing closed (no entry).", e)
            return False

    def check_gross_exposure_for_order(self, tracker, entry_price: float, shares: int) -> bool:
        """Aggregate gross-exposure gate (2026-07-14, board + Gro + GAI): the PRIMARY
        governor of how many positions the account carries now that MAX_OPEN_POSITIONS is a
        runaway-loop circuit-breaker. Blocks a new entry if it would push
        sum(|open position notional|) above config.MAX_GROSS_EXPOSURE_RATIO × equity.

        Notional is summed from the tracker's non-closed open_trades. A malformed single
        row is skipped. On a total read error this FAILS OPEN (returns True) — the
        buying-power pre-flight above is the hard, fail-closed account-level guard, so this
        governor never needs to halt the whole book on a tracker glitch.
        """
        try:
            new_notional = float(shares) * float(entry_price)
            if new_notional <= 0:
                return False   # malformed sizing (shares/price <= 0) — fail-closed (cold-2nd defense-in-depth)
            open_notional = 0.0
            for _t in (getattr(tracker, "open_trades", {}) or {}).values():
                if not isinstance(_t, dict) or _t.get("status") == "closed":
                    continue
                try:
                    _qty = float(_t.get("qty_remaining") or _t.get("qty", 0) or 0)
                    _px  = float(_t.get("entry_price", 0) or 0)
                    open_notional += abs(_qty * _px)
                except (TypeError, ValueError):
                    continue
            # getattr-with-default: MAX_GROSS_EXPOSURE_RATIO is added alongside this in config.py;
            # the 2.5 fallback equals the intended value so the guard still holds even if a partial
            # config import were ever seen — degrade to the correct ratio, never to an unbounded one.
            ratio = float(getattr(config, "MAX_GROSS_EXPOSURE_RATIO", 2.5))
            cap   = self.portfolio_value * ratio
            total = open_notional + new_notional
            if total > cap:
                logger.warning(
                    "GROSS-EXPOSURE cap: open $%.0f + new $%.0f = $%.0f > cap $%.0f "
                    "(%.1fx equity) — blocking entry.",
                    open_notional, new_notional, total, cap, ratio,
                )
                return False
            return True
        except Exception as e:  # RC-3
            logger.warning(
                "gross-exposure check error (%s) — allowing entry (BP pre-flight is the hard guard).",
                e,
            )
            return True

    def calculate_position_size(
        self,
        entry_price: float,
        stop_price: float,
        trade_mode: str,
        risk_pct_override: Optional[float] = None,
    ) -> int:
        """
        Calculate number of shares based on risk per trade.
        Uses dollar risk = portfolio_value * MAX_PORTFOLIO_RISK_PCT.
        Position size = dollar_risk / (entry - stop).
        Returns 0 if position would be too small or risk params are invalid.
        """
        if entry_price <= 0 or stop_price <= 0:
            return 0

        # Finding 3: 0.0 is a valid override (disables sizing) — check is not None
        risk_pct     = (
            risk_pct_override if risk_pct_override is not None
            else config.MAX_PORTFOLIO_RISK_PCT
        )
        dollar_risk  = self.portfolio_value * risk_pct
        risk_per_share = abs(entry_price - stop_price)

        if risk_per_share == 0:
            return 0

        shares = int(dollar_risk / risk_per_share)

        # Sanity: position value capped at 95% of portfolio (Bucket B max allocation)
        max_position_value = self.portfolio_value * 0.95
        max_by_value = int(max_position_value / entry_price)
        shares = min(shares, max_by_value)

        if shares < 1:
            logger.info(f"Position size < 1 share at ${entry_price:.2f}. Skipping.")
            return 0

        logger.info(
            f"Position size: {shares} shares @ ${entry_price:.2f} "
            f"(risk ${dollar_risk:.2f}, stop ${stop_price:.2f})"
        )
        return shares

    def get_stop_and_target(
        self,
        entry_price: float,
        direction: str,
        trade_mode: str,
        atr_value: Optional[float] = None,
        **kwargs,
    ) -> tuple:
        """
        Calculate stop loss and take profit prices.
        Uses ATR-based stops when atr_value is provided (preferred).
        Falls back to fixed percentages if ATR unavailable.
        Returns (stop_price, target_price).
        """
        symbol        = kwargs.get("symbol", "")
        is_leveraged_3x = symbol in getattr(config, "LEVERAGED_3X_TICKERS", set())
        is_leveraged    = (symbol in getattr(config, "LEVERAGED_TICKERS", set())
                           and not is_leveraged_3x)

        if atr_value and atr_value > 0:
            # ── ATR-based stops (preferred) ──────────────────────────────────
            if trade_mode == config.TradeMode.INTRADAY:
                stop_mult   = config.INTRADAY_STOP_ATR_MULT
                target_mult = config.INTRADAY_TARGET_ATR_MULT
            else:
                stop_mult   = config.SWING_STOP_ATR_MULT
                target_mult = config.SWING_TARGET_ATR_MULT

            # ── Volatility tier classification (auto, using realized vol) ────
            rvol = kwargs.get("rvol_20d", 0) or 0
            is_overnight = kwargs.get("overnight", False)
            if rvol >= config.VOLATILITY_TIER_EXTREME_THRESHOLD:
                vol_stop = (
                    config.VOL_TIER_EXTREME_STOP_OVERNIGHT if is_overnight
                    else config.VOL_TIER_EXTREME_STOP_INTRADAY
                )
            elif rvol >= config.VOLATILITY_TIER_HIGH_THRESHOLD:
                vol_stop = (
                    config.VOL_TIER_HIGH_STOP_OVERNIGHT if is_overnight
                    else config.VOL_TIER_HIGH_STOP_INTRADAY
                )
            else:
                vol_stop = (
                    config.VOL_TIER_STD_STOP_OVERNIGHT if is_overnight
                    else config.VOL_TIER_STD_STOP_INTRADAY
                )
            # Use vol tier stop if it's wider than the profile default.
            # Scale target proportionally to preserve R:R — same pattern as VIX fix.
            # Without this, vol_stop widens stop but leaves target unchanged,
            # collapsing R:R below the AB-2 minimum (2.0) and blocking all entries.
            if vol_stop > stop_mult:
                target_mult *= vol_stop / stop_mult
                stop_mult = vol_stop

            # ── VIX stop widening — H2 continuous curve or native step-function ─
            # H2 (param_engine.h2_stop_atr_mult) returns a pure scalar. When active,
            # it replaces the step-function as the sole VIX+RV authority — both
            # stop_mult and target_mult scale by the same scalar, preserving R:R.
            # When atr_mult_override is None (VIX unavailable or H2 not wired),
            # native step-function runs unchanged — zero regression on existing callers.
            vix = kwargs.get("vix", 0) or 0
            h2_scalar = kwargs.get("atr_mult_override", None)
            # AWP audit finding (2026-06-28): h2_stop_atr_mult() returns a pure
            # scalar that can mathematically be exactly 0.0 (not None) when a
            # symbol's realized vol computes to zero variance — e.g. a halted
            # or completely flat stock over the lookback window. Multiplying
            # stop_mult/target_mult by 0.0 produces a zero-width stop AND
            # target (stop = target = entry_price) — the same "penny-stop"
            # class of bug already guarded against elsewhere in this codebase
            # (see C-1's 0.5R breakeven guard). Treat <= 0 the same as None —
            # fall through to the native step-function rather than zeroing
            # out risk management entirely.
            if h2_scalar is not None and h2_scalar > 0:
                stop_mult   *= h2_scalar
                target_mult *= h2_scalar
            elif vix > 0:
                _vix_scalar = 1.0 + max(0.0, vix - 20.0) * 0.1
                _vix_scalar = min(_vix_scalar, 2.0)
                stop_mult   *= _vix_scalar
                target_mult *= _vix_scalar

            # ── ATH proximity scalar (board 3-1 + Gro + GAI, 2026-06-30) ─────
            # When SPY is within 1% of its 52-week high, apply a 10% stop
            # tightening at entry. Rationale: signal quality is noisier right
            # at the ATH boundary; accepting a modestly tighter risk budget
            # preserves R:R (both stop AND target scale by the same 0.90x).
            # Applied AFTER VIX scalar, BEFORE leverage multipliers — fits the
            # existing multiplication chain without disturbing dual-authority.
            # Default spy_ath_dist_pct=99.0 (no data = no penalty).
            _spy_ath_dist_pct = kwargs.get("spy_ath_dist_pct", 99.0) or 99.0
            if _spy_ath_dist_pct < 1.0:
                _ath_scalar  = 0.90
                stop_mult   *= _ath_scalar
                target_mult *= _ath_scalar
                logger.info(
                    f"[{symbol}] ATH proximity scalar applied: SPY {_spy_ath_dist_pct:.2f}%"
                    f" from 52w high → stop/target ×0.90 (R:R preserved)"
                )

            if is_leveraged_3x:
                stop_mult   *= _safe_lev_mult(
                    config.LEVERAGED_3X_STOP_MULTIPLIER,
                    "LEVERAGED_3X_STOP_MULTIPLIER", symbol,
                )
                target_mult *= _safe_lev_mult(
                    config.LEVERAGED_3X_TARGET_MULTIPLIER,
                    "LEVERAGED_3X_TARGET_MULTIPLIER", symbol,
                )
            elif is_leveraged:
                stop_mult   *= _safe_lev_mult(
                    config.LEVERAGED_STOP_MULTIPLIER,
                    "LEVERAGED_STOP_MULTIPLIER", symbol,
                )
                target_mult *= _safe_lev_mult(
                    config.LEVERAGED_TARGET_MULTIPLIER,
                    "LEVERAGED_TARGET_MULTIPLIER", symbol,
                )

            stop_dist   = atr_value * stop_mult
            target_dist = atr_value * target_mult
            rr = round(target_mult / stop_mult, 1)
            lev_tag = (
                " [3X LEVERAGED]" if is_leveraged_3x
                else (" [LEVERAGED]" if is_leveraged else "")
            )
            logger.info(
                f"[{symbol}] ATR-based stop: ${atr_value:.2f} ATR | "
                f"stop {stop_mult}x = ${stop_dist:.2f} | "
                f"target {target_mult}x = ${target_dist:.2f} | R:R 1:{rr}"
                + lev_tag
            )
        else:
            # ── Fallback: fixed percentage stops ─────────────────────────────
            logger.warning(f"[{symbol}] No ATR data — using fixed % stops (fallback)")
            if trade_mode == config.TradeMode.INTRADAY:
                stop_pct   = config.INTRADAY_STOP_PCT
                target_pct = config.INTRADAY_TARGET_PCT
            else:
                stop_pct   = config.SWING_STOP_PCT
                target_pct = config.SWING_TARGET_PCT

            if is_leveraged_3x:
                stop_pct   *= _safe_lev_mult(
                    config.LEVERAGED_3X_STOP_MULTIPLIER,
                    "LEVERAGED_3X_STOP_MULTIPLIER", symbol,
                )
                target_pct *= _safe_lev_mult(
                    config.LEVERAGED_3X_TARGET_MULTIPLIER,
                    "LEVERAGED_3X_TARGET_MULTIPLIER", symbol,
                )
            elif is_leveraged:
                stop_pct   *= _safe_lev_mult(
                    config.LEVERAGED_STOP_MULTIPLIER,
                    "LEVERAGED_STOP_MULTIPLIER", symbol,
                )
                target_pct *= _safe_lev_mult(
                    config.LEVERAGED_TARGET_MULTIPLIER,
                    "LEVERAGED_TARGET_MULTIPLIER", symbol,
                )

            stop_dist   = entry_price * stop_pct
            target_dist = entry_price * target_pct

        if direction == "long":
            stop   = entry_price - stop_dist
            target = entry_price + target_dist
        else:
            stop   = entry_price + stop_dist
            target = entry_price - target_dist

        return round(stop, 2), round(target, 2)

    def register_open(self):
        self.open_positions += 1

    def register_close(self, pnl: float = 0.0):
        pnl = pnl or 0.0   # guard: None callers would TypeError on daily_pnl +=
        self.open_positions = max(0, self.open_positions - 1)
        self.daily_pnl += pnl

    def update_daily_pnl_from_alpaca(self) -> None:
        """
        Phase 2: seed FIFO lot queues from /v2/positions for overnight basis,
        match today's fills chronologically, overwrite self.daily_pnl.
        BV-2 Option A (board unanimous). On any API failure: log warning,
        return without modifying daily_pnl.
        """
        today   = datetime.now(_ET).strftime("%Y-%m-%d")
        api_key = os.getenv("ALPACA_API_KEY", "")
        secret  = os.getenv("ALPACA_SECRET_KEY", "")
        if not api_key or not secret:
            logger.warning(
                "update_daily_pnl_from_alpaca: API keys not set — skipping"
            )
            return

        et_start = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=_ET)
        et_end   = et_start.replace(hour=23, minute=59, second=59)
        headers  = {
            "APCA-API-KEY-ID":     api_key,
            "APCA-API-SECRET-KEY": secret,
        }
        base_url = "https://paper-api.alpaca.markets"

        # ── Step 1: Fetch today's FILL activities (paginated) ─────────────
        all_fills: list = []
        after_id: str | None = None
        # guard: 20×100=2000 fills max; prevents infinite loop on repeated after_id
        _max_pages = 20
        _pages_fetched = 0
        try:
            while _pages_fetched < _max_pages:
                _fmt = "%Y-%m-%dT%H:%M:%SZ"
                params: dict = {
                    "after":     et_start.astimezone(timezone.utc).strftime(_fmt),
                    "until":     et_end.astimezone(timezone.utc).strftime(_fmt),
                    "page_size": 100,
                }
                if after_id:
                    params["after_id"] = after_id
                _pages_fetched += 1
                resp = requests.get(
                    f"{base_url}/v2/account/activities/FILL",
                    headers=headers,
                    params=params,
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    logger.warning(
                        "update_daily_pnl_from_alpaca: fills HTTP %d"
                        " — keeping daily_pnl",
                        resp.status_code,
                    )
                    return
                page = resp.json()
                if not isinstance(page, list) or not page:
                    break
                all_fills.extend(page)
                after_id = page[-1].get("id")
                if len(page) < 100:
                    break
        except Exception as exc:
            logger.warning(
                "update_daily_pnl_from_alpaca: fills error %s"
                " — keeping daily_pnl", exc
            )
            return

        if not all_fills:
            logger.debug(
                "update_daily_pnl_from_alpaca: no fills today (%s)", today
            )
            return

        # ── Step 2: Fetch open positions to seed overnight lot basis ──────
        try:
            resp = requests.get(
                f"{base_url}/v2/positions",
                headers=headers,
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.warning(
                    "update_daily_pnl_from_alpaca: positions HTTP %d"
                    " — keeping daily_pnl",
                    resp.status_code,
                )
                return
            positions = resp.json()
            if not isinstance(positions, list):
                logger.warning(
                    "update_daily_pnl_from_alpaca: positions not a list"
                    " — keeping daily_pnl"
                )
                return
        except Exception as exc:
            logger.warning(
                "update_daily_pnl_from_alpaca: positions error %s"
                " — keeping daily_pnl", exc
            )
            return

        # ── Step 3: Seed FIFO lot queues from open positions ──────────────
        # avg_entry_price is Alpaca-authoritative and FIFO-equivalent for
        # single-tranche entries (no pyramiding in this bot).
        # QHM ownership (Option B step 2, 2026-07-01): the Quarterly Hold Manager
        # is tracked separately and its realized P&L must NOT count toward the
        # intraday kill-switch daily_pnl. Exclude QHM symbols from both the
        # position-seed and the fill-match loops below (lazy import avoids any
        # circular-import risk in this foundational module).
        from execution.quarterly_hold_manager import get_quarterly_hold_symbols
        _qhm_syms = get_quarterly_hold_symbols()
        long_lots: dict  = defaultdict(deque)
        short_lots: dict = defaultdict(deque)
        for pos in positions:
            sym   = pos.get("symbol", "")
            if sym in _qhm_syms:
                continue   # QHM tracked separately — exclude from intraday kill-switch P&L
            side  = pos.get("side", "")
            qty   = float(pos.get("qty", 0) or 0)
            price = float(pos.get("avg_entry_price", 0) or 0)
            if not sym or qty <= 0 or price <= 0:
                continue
            if side == "long":
                long_lots[sym].append({"qty": qty, "price": price})
            elif side == "short":
                short_lots[sym].append({"qty": qty, "price": price})

        # ── Step 4: FIFO match fills in chronological order ───────────────
        realized_pnl    = 0.0
        _unknown_sides: set = set()
        for fill in sorted(
            all_fills, key=lambda f: f.get("transaction_time", "")
        ):
            sym    = fill.get("symbol", "")
            if sym in _qhm_syms:
                continue   # QHM fills excluded from intraday kill-switch daily_pnl
            _side  = fill.get("side", "")
            qty    = float(fill.get("qty", 0) or 0)
            price  = float(fill.get("price", 0) or 0)
            if not sym or qty <= 0 or price <= 0:
                continue

            if _side == "sell_short":
                # Explicit short entry — always opens a new short lot
                short_lots[sym].append({"qty": qty, "price": price})

            elif _side == "buy_to_cover":
                # Explicit short close — always consumes from short queue
                if short_lots.get(sym):
                    remaining = qty
                    while remaining > 0 and short_lots[sym]:
                        lot = short_lots[sym][0]
                        matched = min(lot["qty"], remaining)
                        realized_pnl += (lot["price"] - price) * matched
                        lot["qty"] -= matched
                        remaining  -= matched
                        if lot["qty"] <= 0:
                            short_lots[sym].popleft()
                else:
                    logger.warning(
                        "[%s] buy_to_cover with no short lot — fill skipped,"
                        " P&L not credited", sym
                    )

            elif _side == "buy":
                # Long entry OR legacy short cover (if short lot exists)
                if short_lots.get(sym):
                    remaining = qty
                    while remaining > 0 and short_lots[sym]:
                        lot = short_lots[sym][0]
                        matched = min(lot["qty"], remaining)
                        realized_pnl += (lot["price"] - price) * matched
                        lot["qty"] -= matched
                        remaining  -= matched
                        if lot["qty"] <= 0:
                            short_lots[sym].popleft()
                else:
                    long_lots[sym].append({"qty": qty, "price": price})

            elif _side == "sell":
                if long_lots.get(sym):
                    # Long exit: consume FIFO from long queue
                    remaining = qty
                    while remaining > 0 and long_lots[sym]:
                        lot = long_lots[sym][0]
                        matched = min(lot["qty"], remaining)
                        realized_pnl += (price - lot["price"]) * matched
                        lot["qty"] -= matched
                        remaining  -= matched
                        if lot["qty"] <= 0:
                            long_lots[sym].popleft()
                else:
                    # Alpaca returns sell_short for new short entries; an orphaned
                    # sell with no long lot indicates a stale or erroneous fill.
                    logger.debug("[%s] sell with no long lot — skipping", sym)

            else:
                _unknown_sides.add(_side)

        if _unknown_sides:
            logger.warning(
                "update_daily_pnl_from_alpaca: unknown fill side(s) %s"
                " — those fills were skipped", _unknown_sides
            )

        # ── Step 5: Seed daily_pnl from Alpaca fills — partial-fill guard ─
        # If accumulated daily_pnl is already negative AND Alpaca shows a smaller
        # loss, fills may not be fully settled yet. Keep the bigger loss to ensure
        # the kill switch is never made less sensitive (Thorp/Taleb Option B).
        prev = self.daily_pnl
        alpaca_val = round(realized_pnl, 2)
        if self.daily_pnl >= 0 or alpaca_val <= self.daily_pnl:
            self.daily_pnl = alpaca_val
            guarded = False
        else:
            guarded = True   # keep accumulated loss — partial fill detected
        logger.info(
            "update_daily_pnl_from_alpaca: %d fills → alpaca=$%.2f"
            " (prev=$%.2f, kept=$%.2f%s) [BV-2 Option B] %s",
            len(all_fills), alpaca_val, prev, self.daily_pnl,
            " GUARDED" if guarded else "", today,
        )

    def reset_daily(self, portfolio_value: float):
        """Call this at market open each day."""
        # Finding 8: guard against mid-session reset while kill switch is active.
        # reset_daily() should only fire at SOD; a mid-session call would clear
        # a legitimate kill and restore full trading capacity without operator approval.
        if self.killed:
            logger.critical(
                "reset_daily() called while kill switch is ACTIVE — aborting reset. "
                "Kill switch must be cleared manually before daily reset proceeds."
            )
            return
        self.daily_start_value = portfolio_value
        self.portfolio_value   = portfolio_value
        self.daily_pnl         = 0.0
        self.killed            = False
        # BUG-ADV-1: clear persisted state at daily reset
        _save_kill_state(killed=False, halt_entries=False)
        logger.info(f"Daily reset. Portfolio: ${portfolio_value:,.2f}")

    def sync_from_tracker(self, tracker) -> None:
        """Sync open_positions from tracker at startup — prevents position count drift.
        Idempotent. Call once after both risk and tracker are initialized in main.py.
        """
        if tracker is None:
            logger.warning("sync_from_tracker called with None tracker — skipping")
            return
        open_trades = getattr(tracker, "open_trades", {})
        count = sum(
            1 for t in open_trades.values() if t.get("status") != "closed"
        )
        _max_sane = getattr(config, "MAX_OPEN_POSITIONS", 10) * 2
        if count > _max_sane:
            logger.error(
                f"sync_from_tracker: count {count} exceeds sanity cap {_max_sane} "
                f"— aborting sync, keeping open_positions={self.open_positions}"
            )
            return
        prev = self.open_positions
        self.open_positions = count
        logger.info(
            f"RiskManager synced: open_positions {prev}→{self.open_positions} "
            f"(from tracker.open_trades)"
        )

    def reconcile_open_positions_from_alpaca(self, qhm_syms=None) -> bool:
        """ROBUST mid-cycle counter reconcile (2026-08-01, Rafael) — set open_positions to
        Alpaca's AUTHORITATIVE live count of NON-QHM positions, healing BOTH directions:
          - OVER-count: a close that removed the tracker entry WITHOUT calling register_close()
            (e.g. an external/stop close force-cleaning the tracker) leaves this counter high;
            the entry-logic CYCLE-SYNC guard REFUSES to decrement on a tracker-only signal, so
            the over-count silently blocks a real entry slot until the next restart's resync.
          - UNDER-count: symmetric drift the other way.
        Alpaca /v2/positions is ground truth (one net position per symbol, no per-strategy tag),
        so QHM symbols are EXCLUDED to match this intraday-only counter — the same exclusion the
        CYCLE-SYNC comparison and sync_from_tracker already use.

        Returns True and updates the counter ONLY on a CLEAN read (keys present, HTTP 200, a list
        payload, count within the sanity cap). Returns False WITHOUT modifying the counter on ANY
        failure so the caller falls back to its conservative tracker-based sync-UP-only guard — a
        read failure must NEVER blindly move the entry gate. Mirrors the /v2/positions fetch
        pattern already used by _qhm_unrealized_pl(). NEVER RAISES."""
        try:
            qhm = set(qhm_syms or [])
            api_key = os.getenv("ALPACA_API_KEY", "")
            secret  = os.getenv("ALPACA_SECRET_KEY", "")
            if not api_key or not secret:
                logger.warning(
                    "reconcile_open_positions_from_alpaca: API keys absent — no reconcile "
                    "(caller falls back to tracker sync-up-only)."
                )
                return False
            resp = requests.get(
                f"{_ALPACA_BASE_URL}/v2/positions",
                headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.warning(
                    "reconcile_open_positions_from_alpaca: positions HTTP %d — no reconcile "
                    "(caller falls back).", resp.status_code,
                )
                return False
            positions = resp.json()
            if not isinstance(positions, list):
                logger.warning(
                    "reconcile_open_positions_from_alpaca: positions payload not a list — no "
                    "reconcile (caller falls back)."
                )
                return False
            count = sum(
                1 for p in positions
                if isinstance(p, dict) and p.get("symbol") and p.get("symbol") not in qhm
            )
            _max_sane = getattr(config, "MAX_OPEN_POSITIONS", 10) * 2
            if count > _max_sane:
                logger.error(
                    "reconcile_open_positions_from_alpaca: Alpaca ex-QHM count %d exceeds sanity "
                    "cap %d — no reconcile (caller falls back).", count, _max_sane,
                )
                return False
            prev = self.open_positions
            if count != prev:
                logger.info(
                    "RiskManager reconciled from Alpaca (authoritative): open_positions %d→%d "
                    "(ex-QHM live positions).", prev, count,
                )
            self.open_positions = count
            return True
        except Exception as e:  # RC-3
            logger.warning(
                "reconcile_open_positions_from_alpaca error (%s) — no reconcile "
                "(caller falls back).", e,
            )
            return False

    def get_news_adjusted_stop(
        self,
        stop_price: float,
        entry_price: float,
        direction: str,
        news_size_mult: float,
    ) -> float:
        """
        Tighten the stop loss when news risk is elevated.

        During HIGH_RISK news (size_mult=0.5): tighten stop by 20%
        toward entry — reduces max loss per trade during volatile news.
        During CAUTION (size_mult=0.75): no stop adjustment, size cut is enough.
        During HALT (size_mult=0.0): handled by force-close, not stop adjustment.

        This runs at entry time, not in real-time, so it affects only NEW trades
        opened while news is elevated. Existing bracket stops are unchanged.
        """
        if news_size_mult >= 0.75:
            return stop_price   # CAUTION or clear — no adjustment

        if news_size_mult <= 0.0:
            return stop_price   # HALT handled elsewhere

        # HIGH_RISK: tighten stop 20% toward entry
        tighten_pct = 0.20
        stop_dist   = abs(entry_price - stop_price)
        tighter_dist = stop_dist * (1 - tighten_pct)

        if direction == "long":
            new_stop = round(entry_price - tighter_dist, 2)
        else:
            new_stop = round(entry_price + tighter_dist, 2)

        logger.info(
            f"News HIGH_RISK: stop tightened {tighten_pct:.0%} "
            f"from ${stop_price} → ${new_stop}"
        )
        return new_stop

    # calculate_bucket_allocation() and calculate_bucket_a_size() were REMOVED 2026-07-15
    # (Bucket A/B collapse). They were dead (no callers — entry_logic sizes inline via the
    # unified conviction path) and referenced the deleted BUCKET_*_ALLOCATION_PCT constants.

    def summary(self) -> dict:
        worst_pnl = min(self.daily_pnl, self.equity_pnl)
        return {
            "portfolio_value":   self.portfolio_value,
            "daily_pnl":         self.daily_pnl,
            "equity_pnl":        self.equity_pnl,
            "worst_pnl":         worst_pnl,
            "daily_pnl_pct":     (
                worst_pnl / self.daily_start_value if self.daily_start_value else 0
            ),
            "open_positions":    self.open_positions,
            "kill_switch_active": self.killed,
        }
