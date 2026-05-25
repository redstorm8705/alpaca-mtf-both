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
                    logger.debug(f"Skipping {sym} — trend {trend} vs direction {direction}")
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

            # Place stop loss order
            stop_side = "sell" if direction == "long" else "buy"
            self.broker.place_stop(sym, qty, stop_loss, stop_side)

            # Track internally
            self._active_movers[sym] = {
                "entry_price": price,
                "direction":   direction,
                "take_profit": take_profit,
                "stop_loss":   stop_loss,
                "qty":         qty,
                "mode":        mode,
                "entry_time":  datetime.now(),
                "event_note":  mover.get("event_note", ""),
            }

            # Log to portfolio tracker
            signal_mock = {
                "symbol":    sym,
                "action":    "buy" if direction == "long" else "sell_short",
                "mode":      mode,
                "direction": direction,
                "score":     mover.get("confluence_score", 0) or 0,
                "reason":    f"Movers strategy | {direction} | {mover['pct_change']:+.2f}% | vol {vol_ratio:.1f}x",
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
                    result = self.broker.close_position(sym)
                    if result["success"]:
                        self.tracker.log_exit(sym, curr_price, reason)
                        self._active_movers.pop(sym)
                        closed.append(sym)
                        emoji = "💰" if hit_tp else "🛑"
                        logger.info(f"{emoji} {reason}: {sym} @ ${curr_price:.2f}")

            except Exception as e:
                logger.error(f"Exit check error {sym}: {e}")

        return closed

    def flatten_intraday(self) -> list:
        """Close all intraday movers positions — called at EOD."""
        closed = []
        intraday = [s for s, t in self._active_movers.items() if t["mode"] == "intraday"]

        for sym in intraday:
            try:
                result = self.broker.close_position(sym)
                if result["success"]:
                    pos = self.broker.get_position(sym)
                    price = float(pos.current_price) if pos else self._active_movers[sym]["entry_price"]
                    self.tracker.log_exit(sym, price, "EOD flatten")
                    self._active_movers.pop(sym)
                    closed.append(sym)
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
