# ruff: noqa: E501
"""
execution/kelly.py
Kelly Criterion dynamic position sizing.

Tracks win rate and average win/loss ratio per signal type.
Applies fractional Kelly once minimum sample size is reached.
Falls back to MAX_PORTFOLIO_RISK_PCT until enough data exists.

Signal types tracked:
  "long_intraday", "short_intraday", "long_swing", "short_swing"

Kelly formula:
  f* = (win_rate * avg_win_r - loss_rate * avg_loss_r) / avg_win_r
  where R = return as multiple of risk (e.g. win of $20 on $10 risk = 2.0R)

We use FRACTIONAL Kelly (config.KELLY_FRACTION * f*) to avoid over-betting.
"""

from datetime import datetime, timezone
import json
import logging
import statistics
from pathlib import Path
import config

logger = logging.getLogger("kelly")
KELLY_STATS_FILE = Path(__file__).resolve().parent.parent / "logs" / "kelly_stats.json"
TQI_HISTORY_FILE = Path(__file__).resolve().parent.parent / "logs" / "tqi_history.json"


class KellySizer:
    def __init__(self):
        self._stats = {}          # signal_type -> {"wins": [...R], "losses": [...R]}
        self._ath_equity: float = 0.0  # A2 ATH drawdown tracking
        self._ath_updated_at: str = "" # ISO UTC timestamp of last ATH update
        self._tqi_history: list = []   # rolling per-trade TQI scores (max 10 in-memory, 100 on disk)
        self._load()              # catches all exceptions internally; self._stats stays {} on error
        self._restore_tqi_from_disk()  # unconditional — independent of _load() success (DS Condition 4)
        logger.info(
            f"Kelly config: fraction={config.KELLY_FRACTION} | "
            f"max_risk={config.KELLY_MAX_RISK_PCT:.1%} | "
            f"min_risk={config.KELLY_MIN_RISK_PCT:.1%}"
        )

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self):
        try:
            if not KELLY_STATS_FILE.exists():
                return
            with open(KELLY_STATS_FILE) as f:
                raw = json.load(f)
            # ATH fields stored at top level alongside signal-type keys
            self._ath_equity     = float(raw.get("ath_equity", 0.0))
            self._ath_updated_at = str(raw.get("ath_updated_at", ""))
            if "ath_equity" not in raw:
                logger.warning(
                    "[Kelly] ath_equity missing from kelly_stats.json — "
                    "defaulting to 0.0 (A2 inactive until first equity high-water mark)"
                )
            # Stats: everything except ATH metadata keys; filter malformed entries
            _SKIP = {"ath_equity", "ath_updated_at"}
            self._stats = {
                k: v for k, v in raw.items()
                if k not in _SKIP
                and isinstance(v, dict)
                and "wins" in v
                and "losses" in v
            }
            total = sum(len(v["wins"]) + len(v["losses"]) for v in self._stats.values())
            logger.info(
                f"Kelly: loaded {total} historical trades from {KELLY_STATS_FILE} | "
                f"ath_equity=${self._ath_equity:,.2f}"
            )
        except Exception as e:
            logger.warning(f"Kelly: could not load stats: {e}")
            self._stats       = {}
            self._ath_equity  = 0.0
            self._ath_updated_at = ""

    def _save(self):
        """Atomically write Kelly stats + ATH watermark — crash-safe."""
        import os
        import shutil
        tmp_path = Path(str(KELLY_STATS_FILE) + ".tmp")
        bak_path = Path(str(KELLY_STATS_FILE) + ".bak")
        try:
            KELLY_STATS_FILE.parent.mkdir(exist_ok=True)
            # ATH fields set last — cannot be overwritten by self._stats
            payload = dict(self._stats)
            payload["ath_equity"]     = self._ath_equity
            payload["ath_updated_at"] = self._ath_updated_at
            with open(tmp_path, "w") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            if KELLY_STATS_FILE.exists():
                shutil.copy2(str(KELLY_STATS_FILE), str(bak_path))
            os.replace(str(tmp_path), str(KELLY_STATS_FILE))
        except Exception as e:
            logger.warning(f"Kelly: could not save stats: {e}")
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError as _unl_e:
                logger.debug("Kelly: tmp cleanup failed (orphan .tmp may remain): %s", _unl_e)

    def _a2_mult(self, portfolio_value: float) -> float:
        """ATH drawdown multiplier — scale-DOWN only, range [KELLY_A2_MULT_FLOOR, 1.0].

        Returns 1.0 when ath_equity unset or drawdown < KELLY_A2_DD_START.
        Linear ramp: 1.0 at KELLY_A2_DD_START → KELLY_A2_MULT_FLOOR at KELLY_A2_DD_MAX.
        Board vote 2026-05-16 unanimous (DS + GAI + board).
        """
        if self._ath_equity <= 0.0 or portfolio_value <= 0.0:
            return 1.0
        drawdown = max(0.0, (self._ath_equity - portfolio_value) / self._ath_equity)
        dd_start = getattr(config, "KELLY_A2_DD_START",   0.02)
        dd_max   = getattr(config, "KELLY_A2_DD_MAX",     0.15)
        a2_floor = getattr(config, "KELLY_A2_MULT_FLOOR", 0.33)
        if dd_start >= dd_max:
            logger.error(
                "[Kelly] A2 config invalid: KELLY_A2_DD_START >= KELLY_A2_DD_MAX"
                " — A2 inactive"
            )
            return 1.0
        if drawdown <= dd_start:
            return 1.0
        if drawdown >= dd_max:
            return a2_floor
        t = (drawdown - dd_start) / (dd_max - dd_start)
        return round(1.0 - t * (1.0 - a2_floor), 4)

    def _restore_tqi_from_disk(self) -> None:
        """Restore TQI rolling history on bot restart.

        Phase 0.5: moved from main() startup block. Called unconditionally
        in __init__ — independent of _load() success (DS Condition 4).
        """
        try:
            if TQI_HISTORY_FILE.exists():
                with open(TQI_HISTORY_FILE) as f:
                    loaded = json.load(f)
                if isinstance(loaded, list) and loaded:
                    self._tqi_history = [int(v) for v in loaded[-100:]]
                    logger.info(
                        f"Kelly: restored {len(self._tqi_history)} TQI scores from disk | "
                        f"rolling avg "
                        f"{round(sum(self._tqi_history)/len(self._tqi_history),1):.1f}/100"
                    )
        except Exception as e:
            logger.warning(f"Kelly: could not restore TQI history: {e}")

    def append_tqi(self, tqi_score: int) -> None:
        """Append a TQI score, maintain 10-entry in-memory ring buffer, persist atomically.

        Phase 0.5: replaces inline _tqi_history management in main._record_tqi().
        """
        import os
        self._tqi_history.append(tqi_score)
        if len(self._tqi_history) > 10:
            self._tqi_history = self._tqi_history[-10:]
        try:
            _tmp = Path(str(TQI_HISTORY_FILE) + ".tmp")
            with open(_tmp, "w") as f:
                json.dump(self._tqi_history[-100:], f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(_tmp), str(TQI_HISTORY_FILE))
        except Exception as e:
            logger.debug(f"TQI history persist failed (non-critical): {e}")

    # ── Signal type key ───────────────────────────────────────────────────────

    @staticmethod
    def _key(direction: str, trade_mode: str) -> str:
        return f"{direction}_{trade_mode}"

    # ── Record trade outcome ──────────────────────────────────────────────────

    def record_trade(
        self,
        direction: str,
        trade_mode: str,
        entry_price: float,
        exit_price: float,
        stop_price: float,
        qty: int,
    ):
        """
        Record a completed trade for Kelly tracking.
        Calculates R-multiple: how many times the risk did we win/lose?
        """
        key = self._key(direction, trade_mode)
        if key not in self._stats:
            self._stats[key] = {"wins": [], "losses": []}

        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0:
            return

        if direction == "long":
            pnl_per_share = exit_price - entry_price
        else:
            pnl_per_share = entry_price - exit_price

        r_multiple = pnl_per_share / risk_per_share

        if r_multiple > 0:
            self._stats[key]["wins"].append(round(r_multiple, 3))
            logger.info(f"Kelly [{key}]: +{r_multiple:.2f}R win recorded")
        else:
            self._stats[key]["losses"].append(round(abs(r_multiple), 3))
            logger.info(f"Kelly [{key}]: -{abs(r_multiple):.2f}R loss recorded")

        self._save()

    # ── Calculate Kelly risk fraction ─────────────────────────────────────────

    def get_risk_pct(
        self,
        direction: str,
        trade_mode: str,
        portfolio_value: float,
    ) -> float:
        """
        Returns the recommended risk % of portfolio for this signal type.

        Returns config.MAX_PORTFOLIO_RISK_PCT as fallback until KELLY_MIN_SAMPLE_SIZE
        trades have been recorded for this signal type.
        """
        key = self._key(direction, trade_mode)
        stats = self._stats.get(key, {"wins": [], "losses": []})

        wins   = stats["wins"]
        losses = stats["losses"]
        total  = len(wins) + len(losses)

        # Not enough data yet — use default; still track ATH for A2 readiness
        if total < config.KELLY_MIN_SAMPLE_SIZE:
            remaining = config.KELLY_MIN_SAMPLE_SIZE - total
            if portfolio_value > self._ath_equity:
                self._ath_equity     = portfolio_value
                self._ath_updated_at = datetime.now(timezone.utc).isoformat()
                self._save()
            logger.debug(
                f"Kelly [{key}]: {total}/{config.KELLY_MIN_SAMPLE_SIZE} trades — "
                f"using default {config.MAX_PORTFOLIO_RISK_PCT:.1%} "
                f"({remaining} more needed)"
            )
            return config.MAX_PORTFOLIO_RISK_PCT

        win_rate  = len(wins) / total
        loss_rate = 1 - win_rate

        avg_win_r  = sum(wins) / len(wins)   if wins   else 0.001
        avg_loss_r = sum(losses) / len(losses) if losses else 0.001

        # Full Kelly fraction
        # f* = (W*b - L) / b  where b = avg win R, W = win rate, L = loss rate
        kelly_full = (win_rate * avg_win_r - loss_rate * avg_loss_r) / avg_win_r

        # Apply fractional Kelly from profile
        kelly_fraction = getattr(config, "KELLY_FRACTION", 0.25)

        # Empirical Kelly — CV penalty for R-multiple dispersion (board-approved Apr 9 2026)
        # losses stored as positive abs values in stats; negate to build signed R series
        # CV activates only at 30+ trades — below that, sample σ is statistically unreliable
        combined = wins + [-_loss for _loss in losses]
        cv = 0.0
        if len(combined) >= 30 and len(combined) >= 2:
            mu    = statistics.mean(combined)
            sigma = statistics.stdev(combined)
            if mu > 0 and sigma > 0:
                cv = sigma / mu
        # Cap penalty at 0.5: prevents total Kelly elimination on high-variance small samples
        # (board ruling: min effective fraction = kelly_fraction * 0.5, never zero)
        penalty = min(0.5, max(0.0, cv))

        # A2: ATH drawdown multiplier — board vote 2026-05-16, unanimous DS+GAI+board
        a2_mult = self._a2_mult(portfolio_value)

        # Derman guard: halve CV penalty when both R-multiple dispersion and drawdown
        # are simultaneously elevated — avoids compounding two independent risk signals
        if cv > 0.20 and a2_mult < 0.80:
            penalty = penalty * 0.5
            logger.debug(
                f"Kelly [{key}]: Derman guard — CV={cv:.3f} A2={a2_mult:.3f} "
                f"→ CV penalty halved to {penalty:.3f}"
            )

        empirical_fraction = kelly_fraction * (1.0 - penalty)
        kelly_risk         = kelly_full * empirical_fraction * a2_mult

        # Update ATH on new equity high — runs UNCONDITIONALLY before any early return.
        # Cold second-agent S49: ATH must update even when kelly_full <= 0 so A2 multiplier
        # remains current for the next stratum that HAS positive edge.
        if portfolio_value > self._ath_equity:
            self._ath_equity     = portfolio_value
            self._ath_updated_at = datetime.now(timezone.utc).isoformat()
            self._save()

        # S49 board + meta-audit (Thorp/Taleb/Asness unanimous):
        # Negative Kelly = this stratum has no edge. KELLY_MIN_RISK_PCT floor must NOT
        # apply here — forcing a 0.75% bet on negative expectancy destroys capital.
        # Thorp (Fortune's Formula): f* < 0 means expected value is negative; optimal bet = 0.
        if kelly_full <= 0:
            logger.warning(
                "Kelly [%s]: NEGATIVE EXPECTANCY — kelly_full=%.3f "
                "(WR=%.1%%, avg_win=%.2fR, avg_loss=%.2fR, n=%d). "
                "Blocking %s entries until edge improves (kelly_full > 0).",
                key, kelly_full, win_rate, avg_win_r, avg_loss_r, total, key,
            )
            return 0.0

        # Clamp — KELLY_MIN_RISK_PCT floor only when kelly_full > 0 (positive edge above)
        kelly_risk = max(config.KELLY_MIN_RISK_PCT, min(config.KELLY_MAX_RISK_PCT, kelly_risk))

        logger.info(
            f"Kelly [{key}]: {total} trades | WR {win_rate:.1%} | "
            f"avg win {avg_win_r:.2f}R | avg loss {avg_loss_r:.2f}R | "
            f"full Kelly {kelly_full:.3f} | CV {cv:.3f} | penalty {penalty:.3f} | "
            f"A2 {a2_mult:.3f} | frac {empirical_fraction:.3f} → risk {kelly_risk:.1%}"
        )

        return kelly_risk

    # ── Stats summary ─────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """Returns per-signal-type Kelly stats for dashboard/logging."""
        summary = {}
        for key, stats in self._stats.items():
            wins   = stats["wins"]
            losses = stats["losses"]
            total  = len(wins) + len(losses)
            if total == 0:
                continue
            summary[key] = {
                "total_trades": total,
                "win_rate":     round(len(wins) / total * 100, 1),
                "avg_win_r":    round(sum(wins) / len(wins), 2) if wins else 0,
                "avg_loss_r":   round(sum(losses) / len(losses), 2) if losses else 0,
                "profit_factor": round(
                    sum(wins) / sum(losses) if losses else float("inf"), 2
                ),
                "kelly_ready":  total >= config.KELLY_MIN_SAMPLE_SIZE,
            }
        return summary

    def rebuild_from_trades(self, closed_trades: list):
        """
        Rebuild kelly_stats.json from the corrected closed_trades array.
        Called by portfolio_tracker.write_eod_summary() after Alpaca FIFO
        reconciliation so Kelly sizing reflects accurate historical R-multiples.
        Clears existing stats and batch-rebuilds in a single save.
        """
        self._stats = {}
        rebuilt = 0
        for t in closed_trades:
            direction  = t.get("direction", "")
            trade_mode = t.get("trade_mode", "intraday")
            entry      = float(t.get("entry_price") or 0)
            exit_p     = float(t.get("exit_price") or 0)
            stop       = float(t.get("original_stop") or t.get("stop") or 0)
            qty        = int(t.get("qty") or 1)

            if not direction or entry <= 0 or stop <= 0 or qty <= 0:
                continue

            key = self._key(direction, trade_mode)
            if key not in self._stats:
                self._stats[key] = {"wins": [], "losses": []}

            risk_per_share = abs(entry - stop)
            if risk_per_share <= 0:
                continue

            pnl_per_share = (exit_p - entry) if direction == "long" else (entry - exit_p)
            r_multiple    = pnl_per_share / risk_per_share

            if r_multiple > 0:
                self._stats[key]["wins"].append(round(r_multiple, 3))
            else:
                self._stats[key]["losses"].append(round(abs(r_multiple), 3))
            rebuilt += 1

        self._save()
        total_r = sum(len(v["wins"]) + len(v["losses"]) for v in self._stats.values())
        logger.info(
            f"Kelly: rebuilt from {rebuilt}/{len(closed_trades)} closed trades "
            f"→ {total_r} R-multiples across {len(self._stats)} signal type(s)"
        )

    def print_summary(self):
        summary = self.get_summary()
        if not summary:
            logger.info("Kelly: no trade data yet")
            return
        logger.info("─── Kelly Stats ─────────────────────────────────")
        for key, s in summary.items():
            status = "ACTIVE" if s["kelly_ready"] else f"warming up ({s['total_trades']}/{config.KELLY_MIN_SAMPLE_SIZE})"
            logger.info(
                f"  {key:<20} | {s['total_trades']:>3} trades | "
                f"WR {s['win_rate']:>5.1f}% | "
                f"avg {s['avg_win_r']:.2f}R/{s['avg_loss_r']:.2f}R | "
                f"PF {s['profit_factor']} | {status}"
            )
