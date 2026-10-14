# ruff: noqa: E501  — dense rationale lines run long (project convention)
"""no_static_scan (Rafael 2026-10-14, standing + permanent): guilty by default — every new number in risk/sizing
code is a threshold unless provably mechanics; no PROV/STATIC-WAIVER tag exemption. Cases come from three cold
review rounds."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import no_static_scan as ns  # noqa: E402
import preship_gate as pg  # noqa: E402


def _lits(src):
    return [v.literal for v in ns.scan("x.py", src)]


class Caught(unittest.TestCase):
    def test_the_three_2026_10_14_numbers(self):
        self.assertEqual(_lits("SWING_BREAKOUT_TOTAL_OVERNIGHT_K = 1.9\n"), [1.9])
        self.assertEqual(_lits("SWING_RESERVE_LOTS = 2\n"), [2])
        self.assertEqual(_lits("SWING_BREAKOUT_MIN1_RISK_PCT = 0.03  # PROV:x\n"), [0.03])

    def test_tags_never_exempt(self):
        self.assertEqual(_lits("# STATIC-WAIVER:x\nif vix > 25:\n    pass\n"), [25])

    def test_names_that_used_to_leak(self):
        for line, lit in (("KELLY_FRACTION = 0.25", 0.25), ("SWING_BREAKOUT_STOP_ATR = 2.5", 2.5),
                          ("TIER_CAPITAL_ACCOUNT_GROSS_NORMAL = 1.25", 1.25), ("RSI_OVERBOUGHT = 70", 70),
                          ("SWING_BREAKOUT_TOP_N = 10", 10), ("MAX_PORTFOLIO_RISK_PCT = 0.06", 0.06),
                          ("MAX_SLIPPAGE_PCT = 0.004", 0.004), ("MAX_ENTRIES_PER_DAY = 5", 5),
                          ("MAX_SLOTS_BATCH = 4", 4), ("MAX_HOLD_MINUTES = 90", 90),
                          ("MAX_CAPTURE_RETRIES = 3", 3)):
            self.assertEqual(_lits(line + "\n"), [lit], line)

    def test_shapes_that_used_to_leak(self):
        self.assertEqual(_lits("if tlt_pct < -0.5:\n    pass\n"), [-0.5])
        self.assertEqual(_lits("if 25 < vix:\n    pass\n"), [25])
        self.assertEqual(_lits("if gross > equity * 1.9:\n    pass\n"), [1.9])
        self.assertEqual(_lits("def f(max_lots=4):\n    pass\n"), [4])
        self.assertEqual(_lits("place(qty, max_lots=4)\n"), [4])
        self.assertEqual(_lits("if len(self.lots) >= 4:\n    pass\n"), [4])
        self.assertEqual(_lits("max_notional = equity * 2\n"), [2])
        self.assertEqual(_lits("SWING_RESERVE_LOTS = 2 * 1\n"), [2])
        self.assertEqual(_lits("limits = {'swing': 4}\n"), [4])
        self.assertEqual(_lits("if vix > 25 and len(lots) > 0:\n    pass\n"), [25])
        self.assertEqual(_lits("KELLY = np.float64(0.25)\n"), [0.25])
        self.assertEqual(_lits("cap = pd.Series([2.5]).mean()\n"), [2.5])
        self.assertEqual(_lits("k = derive_k(1.9)\n"), [1.9])

    def test_round4_laundering_inputs(self):
        for src, lits in (("if gross > equity * 1.9 and equity > 0:\n    pass\n", [1.9]),
                          ("if self.qty > 0 and risk_pct > 0.03:\n    pass\n", [0.03]),
                          ("if leverage > 3:\n    pass\n", [3]), ("if average_range > 2.5:\n    pass\n", [2.5]),
                          ("if open_sectors >= 3:\n    pass\n", [3]), ("m = np.clip(raw_mult, 0.5, 1.9)\n", [0.5, 1.9]),
                          ("c = np.minimum(gross, equity * 1.9)\n", [1.9]), ("s = np.where(vix > 25, 0.5, 1.0)\n", [25, 0.5]),
                          ("p = derive_stop(atr, 2.5)\n", [2.5]), ("REENTRY_COOLDOWN_MINUTES = 30\n", [30]),
                          ("EXIT_SECONDS_BEFORE_CLOSE = 900\n", [900]), ("if bb_width < 0.05:\n    pass\n", [0.05]),
                          ("risk_dollars = equity / 100\n", [100]), ("if vix > 25 and vix < 30:\n    pass\n", [25, 30]),
                          ('hot = df[df["atr_pct"] > 0.03]\n', [0.03]), ("picks = ranked[ranked.score >= 9]\n", [9]),
                          ('x = df.loc[df.vix > 25, "sym"]\n', [25]), ("if position_age > 3:\n    pass\n", [3]),
                          ("if minutes_since_open < 15:\n    pass\n", [15]), ("if held_minutes >= 90:\n    pass\n", [90]),
                          ("top = ranked.head(10)\n", [10]), ("if vix > 25 or spy_pct < -1.5:\n    pass\n", [25, -1.5]),
                          ("w = conviction ** 1.5\n", [1.5]),
                          ("if qty == 1 and risk_pct > 0.03:\n    pass\n", [0.03]),
                          ("if shares >= 1 and one_share_risk_pct > 0.03:\n    pass\n", [0.03]),
                          ("if elapsed_minutes > 90:\n    pass\n", [90]), ("if minutes_in_trade > 90:\n    pass\n", [90]),
                          ("if position_age_seconds > 5400:\n    pass\n", [5400]),
                          ("TRADE_TIMEOUT_MINUTES = 45\n", [45]), ("CONFIRM_WAIT_BARS = 3\n", [3]),
                          ("if days_to_expiry <= 2:\n    pass\n", [2]), ("if minutes_to_expiry < 30:\n    pass\n", [30]),
                          ("if total_calls > 5000:\n    pass\n", [5000]), ("if port_beta > 1.5:\n    pass\n", [1.5]),
                          ("if _cache_age <= 1800 and vix > 30:\n    pass\n", [30]),
                          ("if spread_pct > 0.5 and quote_age_seconds < 5:\n    pass\n", [0.5]),
                          ("if resp.status_code == 200 and spy_pct < -1.5:\n    pass\n", [-1.5]),
                          ("if attempt < 3 and risk_pct > 0.03:\n    pass\n", [0.03]),
                          ("if vix > 30 and now.weekday() < 5:\n    pass\n", [30]),
                          ("if len(open_lots) < 2:\n    pass\n", [2]),
                          ("if len(self.lots[sym]) < 2:\n    pass\n", [2]),
                          ("if len(self._lots.get(sym, [])) < 2:\n    pass\n", [2]),
                          ("if len([x for x in lots if x.open]) < 2:\n    pass\n", [2]),
                          ('if len(state["positions"]) < 2:\n    pass\n', [2]),
                          ('if len(self._state.get("lots", {})) < 2:\n    pass\n', [2]),
                          ("if len(self.swing_book) < 2:\n    pass\n", [2]),
                          ("for sym in ranked[:3]:\n    pass\n", [3]), ("entries = signals[:2]\n", [2]),
                          ('top = df.sort_values("score", ascending=False).iloc[:5]\n', [5]),
                          ("if now.minute >= 45 and now.hour == 15:\n    pass\n", [45, 15]),
                          ("capped = qualified[:5]\n", [5]), ("for sym in eligible[:3]:\n    pass\n", [3]),
                          ("todo = symbols[:4]\n", [4]), ("mom = closes[-1] / closes[-21] - 1\n", [-21]),
                          ("x = df['close'].iloc[-20]\n", [-20]), ("top = ranked[2]\n", [2]),
                          ("REENTRY_COOLDOWN_SECONDS = 60 * 60\n", [60, 60]), ("SWING_MAX_HOLD_SECONDS = 24 * 3600\n", [24, 3600]),
                          ("shares = min(raw_shares, 100)\n", [100]), ("shorts = ranked.tail(3)\n", [3]),
                          ("qty = int(1000 / price)\n", [1000]), ("if adv < 1e6 * price:\n    pass\n", [1e6]),
                          ("if spread > price / 1000:\n    pass\n", [1000]), ("budget = equity / 1000\n", [1000]),
                          ("qty = max(0, min(100, raw_shares))\n", [100]),
                          ("budget = day_start_equity / 1000\n", [1000]), ("if adv < 1e6 * day_close:\n    pass\n", [1e6]),
                          ("b = min(cash, equity) / 1000\n", [1000]), ("risk = day_risk_usd * 24\n", [24]),
                          ("if len(longs) < 2:\n    pass\n", [2]), ("if len(self.adds[sym]) < 2:\n    pass\n", [2]),
                          ('k = float(os.environ.get("SWING_K", "1.9"))\n', [1.9]), ('r = Decimal("0.03")\n', [0.03])):
            self.assertEqual(_lits(src), lits, src)


class Mechanics(unittest.TestCase):
    def test_allowed(self):
        for line in ("risk_limit = 0", "cap_mult = np.quantile(vols, 0.9)", "x = df.rolling(20).mean()",
                     "if vix > 25 and spy_pct <= -1.0:\n    pass", "size_pct = round(raw, 2)", "pct = change_pct / 100",
                     "if cache_age > 300:\n    pass",
                     "if len(bars) < 2:\n    pass", "part = text.split(\",\")[2]", "short = headline[:120]", "day = str(ts)[:10]",
                     "for _ in range(3):\n    pass", "time.sleep(0.25)", "pct = frac * 100",
                     "secs = mins * 60", "hold_s = hours * 3600", "m = elapsed.total_seconds() / 60", "h = delta.days * 24", "var = x ** 2", "vol = v ** 0.5", "last = closes[-1]", "if abs(total - 1.0) > 1e-9:\n    pass",
                     "RETRIES = 3", "PAGE_SIZE = 100",
                     "ALPACA_REQ_PER_MINUTE = 175", "if attempt >= 3:\n    pass", "if resp.status_code == 429:\n    pass",
                     "requests.get(url, timeout=15)", "def poll(timeout_s=15):\n    pass", "QUOTE_FETCH_TIMEOUT_SECONDS = 8",
                     "API_RETRY_WAIT_SECONDS = 2", 'QUOTE_FETCH_TIMEOUT_SECONDS = float(os.getenv("QFT", "8"))',
                     'RETRIES = int(os.getenv("R", "3"))', 'best = float("inf")', 'n = int(s, 16)',
                     "score = max(0, min(100, raw))", "score = min(100, max(0, raw))", "m = closes.tail(20).mean()"):
            self.assertEqual(_lits(line + "\n"), [], line)


class Scope(unittest.TestCase):
    def test_every_tier_sizing_file_is_scanned(self):
        for f in ("config.py", "execution/swing_breakout_manager.py", "execution/quarterly_hold_manager.py",
                  "execution/forever_hold_manager.py", "execution/day_trade_manager.py",
                  "execution/day_promotion.py", "execution/tier_capital_allocator.py", "run_day_tier.py",
                  "execution/orphan_manager.py", "execution/param_engine.py", "data/market_breadth.py",
                  "data/gex.py", "execution/lifecycle.py", "execution/reentry_cooldown.py", "events/calendar.py",
                  "execution/counter_trend.py", "execution/gtc_manager.py", "execution/mr_regime.py",
                  "execution/some_new_module.py", "run_swing.py"):
            self.assertTrue(pg._in_risk_scope(f), f)
        for f in ("execution/broker.py", "execution/fifo_pnl.py", "data/fetcher.py", "scan_to_html.py"):
            self.assertFalse(pg._in_risk_scope(f), f)


if __name__ == "__main__":
    unittest.main()
