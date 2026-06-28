# strategy/movers/strategy.py — Top Movers execution strategy
# Fixed exits: +5% take profit / -1% stop loss
# Supports both long (gainers) and short (losers)
# Optionally gates entries on MTF confluence from main strategy

from datetime import datetime
from events.calendar import EventRisk
from data.alpaca_data import get_latest_quote
import logging

logger = logging.getLogger("movers_strategy")

# ─── STRATEGY PARAMETERS ─────────────────────────────────────────────────────
TAKE_PROFIT_PCT   = 0.05   # 5% gain → close
STOP_LOSS_PCT     = 0.01   # 1% loss → close
MIN_CONFLUENCE    = 4      # Minimum MTF score to enter (0 = momentum-only, no filter)
REQUIRE_TREND_ALIGN = True  # Only trade if daily trend matches direction
MIN_VOL_RATIO     = 1.2    # Volume must be 1.2x average (confirms momentum)
MAX_PRICE         = 1000   # Skip very high-priced stocks (sizing issues)
MIN_PRICE         = 5      # Skip penny stocks
MAX_POSITIONS_MOVERS = 5   # Max simultaneous movers positions


class MoversStrategy:
    """
    Executes the Top 10 Movers strategy with fixed % exits.

    Entry logic:
        - Symbol is in top N movers by % change (daily or intraday)
        - Not in event blackout
        - Volume ratio confirms momentum
        - Optional: MTF confluence score meets minimum
        - Optional: Daily trend aligns with trade direction

    Exit logic:
        - Take profit: +5% from entry
        - Stop loss:   -1% from entry
        - EOD flatten: intraday positions closed 15 min before close
    """

    def __init__(self, broker, risk_manager, tracker, calendar, scanner):
        self.broker   = broker
        self.risk     = risk_manager
        self.tracker  = tracker
        self.calendar = calendar
        self.scanner  = scanner
        self._active_movers = {}  # symbol → {entry_price, direction, tp, sl, qty}

    # ─── STARTUP RECONCILIATION ─────────────────────────────────────────────────

    def reconcile_on_startup(self) -> list:
        """
        Rebuild self._active_movers from Alpaca's actual account state.

        2026-06-28 redesign (Rafael mandate): _active_movers is plain in-memory
        state with zero persistence. Every restart (including the crash path
        run_movers.py's main loop now takes on any unexpected exception) would
        otherwise leave the script blind to positions it already opened, even
        though they're still real, live positions at the broker. Mirrors (in
        miniature) execution/orphan_manager.py's reconcile_positions() pattern
        for the main bot.

        For each open Alpaca position not already tracked:
          - Recover entry_price/direction/qty directly from the position.
          - Recover stop_loss from a matching open GTC/DAY stop order if one
            exists; otherwise recompute from STOP_LOSS_PCT and submit a fresh
            GTC stop (the position was found unprotected — treat as an
            emergency-stop case, same posture as orphan_manager.py's Patch 1).
          - take_profit is never stored at the broker — always recomputed from
            entry_price via TAKE_PROFIT_PCT.
          - mode defaults to "intraday" (the conservative choice: an unknown
            recovered position will be flattened at EOD if not closed by
            TP/SL first, rather than silently held overnight indefinitely).
          - entry_time is set to the adoption time (now) — acceptable since
            TP/SL math here depends only on entry_price, not entry_time.

        Returns the list of symbols adopted.
        """
        adopted: list = []
        try:
            positions = self.broker.get_open_positions() or []
        except Exception as e:
            logger.error(f"reconcile_on_startup: get_open_positions failed: {e}")
            return adopted

        try:
            open_orders = self.broker.get_open_orders() or []
        except Exception as e:
            logger.warning(
                f"reconcile_on_startup: get_open_orders failed ({e}) — "
                f"proceeding without existing-stop lookup, will resubmit "
                f"fresh stops for every adopted position."
            )
            open_orders = []

        for pos in positions:
            sym = getattr(pos, "symbol", None)
            if not sym or sym in self._active_movers:
                continue
            try:
                raw_qty   = float(pos.qty)
                qty       = abs(int(raw_qty))
                direction = "long" if raw_qty > 0 else "short"
                entry_price = float(pos.avg_entry_price)
            except (TypeError, ValueError, AttributeError) as e:
                logger.error(
                    f"[{sym}] reconcile_on_startup: position fields invalid "
                    f"({e}) — skipping adoption, manual review required."
                )
                continue
            if qty < 1 or entry_price <= 0:
                logger.error(
                    f"[{sym}] reconcile_on_startup: qty={qty} entry_price="
                    f"{entry_price} invalid — skipping adoption."
                )
                continue

            stop_side = "sell" if direction == "long" else "buy"

            # Look for an existing stop order on this symbol matching our side.
            existing_stop_id = None
            existing_stop_price = None
            for o in open_orders:
                if str(getattr(o, "symbol", "")) != sym:
                    continue
                o_type = str(getattr(o, "order_type", getattr(o, "type", ""))).lower()
                o_side = str(getattr(o, "side", "")).lower()
                if "stop" in o_type and o_side.endswith(stop_side):
                    existing_stop_id = str(getattr(o, "id", "")) or None
                    try:
                        existing_stop_price = float(getattr(o, "stop_price", 0) or 0)
                    except (TypeError, ValueError):
                        existing_stop_price = None
                    break

            stop_order_id: "str | None" = None
            if existing_stop_id and existing_stop_price and existing_stop_price > 0:
                stop_loss = existing_stop_price
                stop_order_id = existing_stop_id
                logger.info(
                    f"[{sym}] reconcile_on_startup: adopted existing stop "
                    f"{stop_order_id} @ ${stop_loss:.2f}"
                )
            else:
                # No existing protective stop found — emergency resubmit,
                # mirroring orphan_manager.py's Patch 1 posture for the main bot.
                stop_loss = round(
                    entry_price * (1 - STOP_LOSS_PCT) if direction == "long"
                    else entry_price * (1 + STOP_LOSS_PCT),
                    2,
                )
                logger.critical(
                    f"[{sym}] reconcile_on_startup: NO existing stop order "
                    f"found — position was unprotected. Submitting emergency "
                    f"GTC stop @ ${stop_loss:.2f}."
                )
                try:
                    resub = self.broker.place_stop(sym, qty, stop_loss, stop_side)
                    stop_order_id = str(resub.id) if resub else None
                except Exception as e:
                    logger.error(f"[{sym}] Emergency stop resubmit failed: {e}")
                    stop_order_id = None
                if stop_order_id is None:
                    logger.critical(
                        f"[{sym}] Emergency stop resubmit FAILED — position "
                        f"UNPROTECTED. Manual action required in Alpaca."
                    )

            take_profit = round(
                entry_price * (1 + TAKE_PROFIT_PCT) if direction == "long"
                else entry_price * (1 - TAKE_PROFIT_PCT),
                2,
            )

            self._active_movers[sym] = {
                "entry_price":   entry_price,
                "direction":     direction,
                "take_profit":   take_profit,
                "stop_loss":     stop_loss,
                "qty":           qty,
                "mode":          "intraday",
                "entry_time":    datetime.now(),
                "event_note":    "adopted at startup — reconcile_on_startup()",
                "stop_order_id": stop_order_id,
            }
            adopted.append(sym)
            logger.warning(
                f"[{sym}] reconcile_on_startup: ADOPTED {direction} {qty}sh "
                f"@ ${entry_price:.2f} | TP ${take_profit:.2f} | SL ${stop_loss:.2f}"
            )

        if adopted:
            logger.warning(
                f"reconcile_on_startup: adopted {len(adopted)} position(s) "
                f"from a prior session: {adopted}"
            )
        else:
            logger.info("reconcile_on_startup: no untracked open positions found.")
        return adopted

    # ─── ENTRY ────────────────────────────────────────────────────────────────

    def evaluate_entries(self, movers: dict, mode: str = "intraday") -> list:
        """
        Evaluates mover list for entries. Returns list of executed trades.
        movers: output from MoversScanner.get_daily_movers() or get_intraday_movers()
        mode: "intraday" (flat EOD) or "swing" (hold overnight)
        """
        executed = []

        all_candidates = (
            [("long",  m) for m in movers["long"]] +
            [("short", m) for m in movers["short"]]
        )

        for direction, mover in all_candidates:
            sym = mover["symbol"]

            # ── Gate: max movers positions ────────────────────────────────
            if len(self._active_movers) >= MAX_POSITIONS_MOVERS:
                logger.info(f"Max movers positions reached ({MAX_POSITIONS_MOVERS})")
                break

            # ── Gate: already in this symbol ──────────────────────────────
            if sym in self._active_movers or self.risk.already_in_position(sym):
                continue

            # ── Gate: event blackout ──────────────────────────────────────
            if mover["event_risk"] == EventRisk.BLACKOUT.value:
                logger.info(f"Skipping {sym} — event blackout: {mover['event_note']}")
                continue

            # ── Gate: volume confirmation ─────────────────────────────────
            vol_ratio = mover.get("vol_ratio", 1.0)
            if vol_ratio < MIN_VOL_RATIO:
                logger.debug(f"Skipping {sym} — low vol ratio: {vol_ratio:.1f}x")
                continue

            # ── Gate: price range ─────────────────────────────────────────
            price = mover["price"]
            if not (MIN_PRICE <= price <= MAX_PRICE):
                continue

            # ── Gate: trend alignment (optional) ─────────────────────────
            if REQUIRE_TREND_ALIGN:
                trend = mover.get("confluence_trend", "unknown")
                if trend not in ("unknown", direction, "neutral"):
                    logger.debug(
                        f"Skipping {sym} — trend {trend} vs direction {direction}"
                    )
                    continue

            # ── Gate: confluence score (optional) ────────────────────────
            if MIN_CONFLUENCE > 0:
                score = mover.get("confluence_score")
                if score is not None and score < MIN_CONFLUENCE:
                    logger.debug(f"Skipping {sym} — low confluence: {score}/{8}")
                    continue

            # ── Gate: portfolio risk ──────────────────────────────────────
            can_trade, reason = self.risk.can_open_trade()
            if not can_trade:
                logger.warning(f"Risk gate blocked: {reason}")
                break

            # ── Calculate exits ───────────────────────────────────────────
            size_mult = mover.get("size_mult", 1.0)
            if direction == "long":
                take_profit = round(price * (1 + TAKE_PROFIT_PCT), 2)
                stop_loss   = round(price * (1 - STOP_LOSS_PCT),   2)
            else:
                take_profit = round(price * (1 - TAKE_PROFIT_PCT), 2)
                stop_loss   = round(price * (1 + STOP_LOSS_PCT),   2)

            base_qty = self.risk.get_position_size(price, stop_loss)
            qty = max(1, int(base_qty * size_mult))

            # ── Execute ───────────────────────────────────────────────────
            if direction == "long":
                result = self.broker.buy(sym, qty, price)
            else:
                result = self.broker.sell_short(sym, qty, price)

            if not result["success"]:
                logger.error(f"Order failed {sym}: {result.get('error')}")
                continue

            # Place stop loss order (GTC — survives a script restart/crash)
            stop_side = "sell" if direction == "long" else "buy"
            stop_order = self.broker.place_stop(sym, qty, stop_loss, stop_side)
            stop_order_id = str(stop_order.id) if stop_order else None
            if stop_order_id is None:
                logger.critical(
                    f"[{sym}] GTC stop FAILED at entry — position unprotected. "
                    f"Manual review required in Alpaca."
                )

            # Track internally
            self._active_movers[sym] = {
                "entry_price":   price,
                "direction":     direction,
                "take_profit":   take_profit,
                "stop_loss":     stop_loss,
                "qty":           qty,
                "mode":          mode,
                "entry_time":    datetime.now(),
                "event_note":    mover.get("event_note", ""),
                "stop_order_id": stop_order_id,
            }

            # Log to portfolio tracker
            signal_mock = {
                "symbol":    sym,
                "action":    "buy" if direction == "long" else "sell_short",
                "mode":      mode,
                "direction": direction,
                "score":     mover.get("confluence_score", 0) or 0,
                "reason":    (
                    f"Movers strategy | {direction} | "
                    f"{mover['pct_change']:+.2f}% | vol {vol_ratio:.1f}x"
                ),
                "stops":     {"stop_loss": stop_loss, "take_profit": take_profit},
            }
            self.tracker.log_entry(signal_mock, qty, price)

            executed.append(sym)
            logger.info(
                f"✓ MOVERS {direction.upper()} {qty}x {sym} @ ${price:.2f} | "
                f"TP: ${take_profit:.2f} (+5%) | SL: ${stop_loss:.2f} (-1%) | "
                f"Vol: {vol_ratio:.1f}x"
            )

        return executed

    # ─── EXIT CHECK ───────────────────────────────────────────────────────────

    def check_exits(self) -> list:
        """
        Checks all active movers positions against TP/SL levels.
        Returns list of closed symbols.
        """
        closed = []

        for sym, trade in list(self._active_movers.items()):
            try:
                # Get current price
                quote = None
                try:
                    quote = get_latest_quote(sym)
                    if quote is None:
                        raise ValueError("get_latest_quote returned None")
                    curr_price = (quote["bid"] + quote["ask"]) / 2
                except Exception:
                    # Fallback: get from position
                    pos = self.broker.get_position(sym)
                    if pos is None:
                        self._active_movers.pop(sym, None)
                        continue
                    curr_price = float(pos.current_price)

                direction  = trade["direction"]
                tp         = trade["take_profit"]
                sl         = trade["stop_loss"]

                hit_tp = (direction == "long"  and curr_price >= tp) or \
                         (direction == "short" and curr_price <= tp)
                hit_sl = (direction == "long"  and curr_price <= sl) or \
                         (direction == "short" and curr_price >= sl)

                if hit_tp or hit_sl:
                    reason = f"{'TAKE PROFIT +5%' if hit_tp else 'STOP LOSS -1%'}"
                    # Cancel the resting GTC stop BEFORE closing — Alpaca holds
                    # the qty for an open stop order (held_for_orders); closing
                    # first risks a 40310000 rejection, and leaving the stop
                    # uncancelled orphans a live GTC order with no position
                    # behind it (2026-06-28 redesign, mirrors exit_logic.py's
                    # CRITICAL-1 cancel-before-close pattern for the main bot).
                    self._cancel_stop_if_present(sym, trade)
                    result = self.broker.close_position(sym)
                    if result["success"]:
                        self.tracker.log_exit(sym, curr_price, reason)
                        self._active_movers.pop(sym)
                        closed.append(sym)
                        emoji = "💰" if hit_tp else "🛑"
                        logger.info(f"{emoji} {reason}: {sym} @ ${curr_price:.2f}")
                    else:
                        # Close failed AFTER the stop was cancelled — position
                        # is now naked. Best-effort resubmit to restore
                        # protection rather than leaving it fully unprotected.
                        logger.error(
                            f"[{sym}] close_position failed after {reason} signal "
                            f"and stop cancel — resubmitting stop to avoid a "
                            f"naked position. Manual review required."
                        )
                        _resub_side = "sell" if direction == "long" else "buy"
                        _resub = self.broker.place_stop(
                            sym, trade["qty"], sl, _resub_side
                        )
                        trade["stop_order_id"] = str(_resub.id) if _resub else None
                        if _resub is None:
                            logger.critical(
                                f"[{sym}] Stop resubmit FAILED after close failure — "
                                f"position UNPROTECTED. Manual action required "
                                f"in Alpaca."
                            )

            except Exception as e:
                logger.error(f"Exit check error {sym}: {e}")

        return closed

    def _cancel_stop_if_present(self, sym: str, trade: dict) -> None:
        """Cancel the tracked GTC stop order for sym, if one is recorded.

        Best-effort: a cancel failure is logged but does not block the
        caller from proceeding to close_position() — cancel_order() already
        treats "already filled/cancelled" as success (see broker.py).
        """
        order_id = trade.get("stop_order_id")
        if not order_id:
            return
        try:
            ok = self.broker.cancel_stop_order(order_id)
            if not ok:
                logger.warning(
                    f"[{sym}] Stop order {order_id} cancel returned False — "
                    f"may still be live. Proceeding to close anyway."
                )
        except Exception as e:
            logger.warning(f"[{sym}] Stop order {order_id} cancel raised: {e}")
        trade["stop_order_id"] = None

    def flatten_intraday(self) -> list:
        """Close all intraday movers positions — called at EOD."""
        closed = []
        intraday = [
            s for s, t in self._active_movers.items() if t["mode"] == "intraday"
        ]

        for sym in intraday:
            try:
                trade = self._active_movers[sym]
                self._cancel_stop_if_present(sym, trade)
                result = self.broker.close_position(sym)
                if result["success"]:
                    pos = self.broker.get_position(sym)
                    price = float(pos.current_price) if pos else trade["entry_price"]
                    self.tracker.log_exit(sym, price, "EOD flatten")
                    self._active_movers.pop(sym)
                    closed.append(sym)
                else:
                    # Close failed after the stop was cancelled — this is the
                    # EOD path, so a naked position here risks sitting fully
                    # unprotected overnight. Resubmit best-effort.
                    logger.error(
                        f"[{sym}] EOD flatten close_position failed after stop "
                        f"cancel — resubmitting stop to avoid an overnight "
                        f"naked position. Manual review required."
                    )
                    _resub_side = "sell" if trade["direction"] == "long" else "buy"
                    _resub = self.broker.place_stop(
                        sym, trade["qty"], trade["stop_loss"], _resub_side
                    )
                    trade["stop_order_id"] = str(_resub.id) if _resub else None
                    if _resub is None:
                        logger.critical(
                            f"[{sym}] EOD stop resubmit FAILED — position "
                            f"UNPROTECTED overnight. Manual action required."
                        )
            except Exception as e:
                logger.error(f"EOD flatten error {sym}: {e}")

        return closed

    def get_active_positions(self) -> dict:
        return self._active_movers.copy()

    def print_positions(self):
        if not self._active_movers:
            print("  No active movers positions")
            return
        print(f"\n  {'Symbol':<8} {'Dir':<6} {'Entry':>8} {'TP':>8} {'SL':>8} {'Mode'}")
        print(f"  {'-'*50}")
        for sym, t in self._active_movers.items():
            print(
                f"  {sym:<8} {t['direction']:<6} "
                f"${t['entry_price']:>7.2f} "
                f"${t['take_profit']:>7.2f} "
                f"${t['stop_loss']:>7.2f} "
                f"{t['mode']}"
            )
