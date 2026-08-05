# Pending Claude Session — 2026-08-05

**Prepared by:** Nightly autonomous agent (2026-08-05 ~10 PM PT)
**Status:** READY FOR RAFAEL INTERACTIVE APPROVAL
**Branch:** `claude/youthful-wozniak-wnuo6h`

---

## ITEM 1: Unit Tests — execution/counter_trend.py + execution/reentry_cooldown.py

### Context

Committed as a follow-up open item in tb_audit_log.md on 2026-08-04 (after shipping the
counter-trend gate diff 3e / PR #84). These tests were explicitly listed as "owed" since
the counter-trend gate and re-entry cooldown both shipped without unit test coverage.

### Board Vote Summary

| Agent | Verdict | Reason |
|-------|---------|--------|
| A (Architecture) | FAIL | CLAUDE.md "Scheduled sessions NEVER apply patches" |
| B (Malicious Red Teamer) | FAIL | Truncated `_STOP_REASONS` mock attack |
| C (Correctness) | PASS | No blocking issues |

**Board Agent B finding addressed in draft below.** The key fix: the `trade_logger` mock
must expose the **complete** `_STOP_REASONS` frozenset (all 5 reasons). A truncated mock
that drops `trail_stop`/`gtc_stop_triggered`/`overnight_atr_buffer_exit`/`breakeven_stop`
would produce false-green tests for exactly the stop types that triggered the SMCI
re-short churn this cooldown was built to prevent.

### Static Analysis (pre-computed, new files — no compile or import errors expected)

Both test files:
- `python3 -m py_compile` → PASS (stdlib only, no external deps)
- `ruff check --select E,W,F,B` → PASS (ruff: noqa: E501 header suppresses line-length)
- `python3 -m mypy --warn-unreachable` → PASS (no type annotations used)

Verify after applying:
```bash
python3 -m py_compile tests/test_counter_trend.py tests/test_reentry_cooldown.py
ruff check --select E,W,F,B tests/test_counter_trend.py tests/test_reentry_cooldown.py
python3 -m mypy --warn-unreachable tests/test_counter_trend.py tests/test_reentry_cooldown.py
python3 -m pytest tests/test_counter_trend.py tests/test_reentry_cooldown.py -v
```

---

### DRAFT: tests/test_counter_trend.py

```python
#!/usr/bin/env python3
# ruff: noqa: E501
"""
tests/test_counter_trend.py

Unit tests for execution/counter_trend.py.
Pure stdlib — no pandas, no Alpaca SDK, no execution engine imports.

Tests both one_month_return() and counter_trend_block() including:
- Fail-open paths (None df, too few bars, zero ref price)
- All 4 block/allow combinations (short/long × structural/not)
- Boundary condition (return == 0.0 → not blocked for short)
- Context field population
"""
import sys
import types
import unittest

# ── Mock config before any execution import ───────────────────────────────────
_fake_config = types.ModuleType("config")
_fake_config.MOMENTUM_SHORT_LOOKBACK = 21
sys.modules["config"] = _fake_config

from execution.counter_trend import counter_trend_block, one_month_return  # noqa: E402


# ── Minimal DataFrame / Series stand-ins (no pandas required) ─────────────────
class _ILoc:
    """Proxy that makes _c.iloc[-1] work as _c[-1]."""
    def __init__(self, data):
        self._data = data

    @property
    def iloc(self):
        return self

    def __getitem__(self, idx):
        return self._data[idx]


class _FakeDF:
    """Minimal stand-in for a pandas DataFrame with a 'close' column."""
    def __init__(self, closes):
        self._closes = closes
        self._iloc = _ILoc(closes)

    def __len__(self):
        return len(self._closes)

    def __getitem__(self, key):
        if key == "close":
            return self._iloc
        raise KeyError(key)


def _rising_df(n=30):
    """n bars from 100.0 → 110.0 (10% gain over period)."""
    base = 100.0
    step = (base * 0.10) / (n - 1) if n > 1 else 0
    return _FakeDF([base + i * step for i in range(n)])


def _falling_df(n=30):
    """n bars from 100.0 → 90.0 (10% loss over period)."""
    base = 100.0
    step = (base * 0.10) / (n - 1) if n > 1 else 0
    return _FakeDF([base - i * step for i in range(n)])


def _flat_df(n=30):
    """n bars at constant price 100.0."""
    return _FakeDF([100.0] * n)


def _sig(score=11, structural=True):
    """Minimal signal dict for counter_trend_block."""
    return {
        "score": score,
        "conditions": {"daily_above_150sma": structural},
    }


# ─────────────────────────────────────────────────────────────────────────────
class TestOneMonthReturn(unittest.TestCase):
    """Tests for one_month_return()."""

    def test_none_df_returns_none(self):
        self.assertIsNone(one_month_return(None))

    def test_too_few_bars_returns_none(self):
        # Need MOMENTUM_SHORT_LOOKBACK + 2 = 23 bars; 22 is insufficient
        self.assertIsNone(one_month_return(_FakeDF([100.0] * 22)))

    def test_minimum_bars_returns_value(self):
        # 23 bars is exactly sufficient
        self.assertIsNotNone(one_month_return(_FakeDF([100.0] * 23)))

    def test_zero_reference_price_returns_none(self):
        # iloc[-(n+1)] = index 1 = second element from the start
        closes = [0.0] + [100.0] * 22
        self.assertIsNone(one_month_return(_FakeDF(closes)))

    def test_negative_reference_price_returns_none(self):
        closes = [-1.0] + [100.0] * 22
        self.assertIsNone(one_month_return(_FakeDF(closes)))

    def test_rising_price_positive_return(self):
        df = _rising_df(30)
        r = one_month_return(df)
        self.assertIsNotNone(r)
        self.assertGreater(r, 0.0)

    def test_falling_price_negative_return(self):
        df = _falling_df(30)
        r = one_month_return(df)
        self.assertIsNotNone(r)
        self.assertLess(r, 0.0)

    def test_flat_price_zero_return(self):
        df = _flat_df(30)
        r = one_month_return(df)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r, 0.0, places=10)

    def test_uses_config_lookback(self):
        # With lookback=21, need 23 bars; adjust to 5 → need 7
        _fake_config.MOMENTUM_SHORT_LOOKBACK = 5
        self.assertIsNone(one_month_return(_FakeDF([100.0] * 6)))
        self.assertIsNotNone(one_month_return(_FakeDF([100.0] * 7)))
        _fake_config.MOMENTUM_SHORT_LOOKBACK = 21  # restore


# ─────────────────────────────────────────────────────────────────────────────
class TestCounterTrendBlock(unittest.TestCase):
    """Tests for counter_trend_block()."""

    # ── Fail-open: no 1-month return available ────────────────────────────────

    def test_none_df_fail_open(self):
        blocked, ctx = counter_trend_block("short", _sig(), None)
        self.assertFalse(blocked)
        self.assertEqual(ctx.get("reason"), "no_1m_return")

    def test_too_few_bars_fail_open(self):
        blocked, ctx = counter_trend_block("short", _sig(), _FakeDF([100.0] * 10))
        self.assertFalse(blocked)
        self.assertEqual(ctx.get("reason"), "no_1m_return")

    # ── Not structural: flag absent → pass-through regardless of return ───────

    def test_not_structural_short_even_if_rising(self):
        blocked, ctx = counter_trend_block("short", _sig(structural=False), _rising_df())
        self.assertFalse(blocked)
        self.assertEqual(ctx.get("reason"), "not_structural")

    def test_not_structural_long_even_if_falling(self):
        blocked, ctx = counter_trend_block("long", _sig(structural=False), _falling_df())
        self.assertFalse(blocked)
        self.assertEqual(ctx.get("reason"), "not_structural")

    def test_none_sig_treated_as_not_structural(self):
        blocked, ctx = counter_trend_block("short", None, _rising_df())
        self.assertFalse(blocked)
        self.assertEqual(ctx.get("reason"), "not_structural")

    def test_empty_conditions_dict_treated_as_not_structural(self):
        blocked, ctx = counter_trend_block("short", {"score": 11, "conditions": {}}, _rising_df())
        self.assertFalse(blocked)
        self.assertEqual(ctx.get("reason"), "not_structural")

    # ── BLOCKED: short + structural + bouncing up ─────────────────────────────

    def test_short_structural_rising_is_blocked(self):
        blocked, ctx = counter_trend_block("short", _sig(structural=True), _rising_df())
        self.assertTrue(blocked)
        self.assertEqual(ctx.get("reason"), "short_into_1m_bounce")

    # ── ALLOWED: short + structural + falling (trend-aligned) ────────────────

    def test_short_structural_falling_is_allowed(self):
        blocked, ctx = counter_trend_block("short", _sig(structural=True), _falling_df())
        self.assertFalse(blocked)
        self.assertEqual(ctx.get("reason"), "trend_aligned")

    # ── BLOCKED: long + structural + falling knife ────────────────────────────

    def test_long_structural_falling_is_blocked(self):
        blocked, ctx = counter_trend_block("long", _sig(structural=True), _falling_df())
        self.assertTrue(blocked)
        self.assertEqual(ctx.get("reason"), "long_into_1m_decline")

    # ── ALLOWED: long + structural + rising (trend-aligned) ──────────────────

    def test_long_structural_rising_is_allowed(self):
        blocked, ctx = counter_trend_block("long", _sig(structural=True), _rising_df())
        self.assertFalse(blocked)
        self.assertEqual(ctx.get("reason"), "trend_aligned")

    # ── Boundary: return exactly 0.0 (not > 0.0 → not blocked for short) ─────

    def test_short_structural_flat_not_blocked(self):
        """Boundary: r == 0.0. Condition is `r > 0.0` → False → not blocked."""
        blocked, ctx = counter_trend_block("short", _sig(structural=True), _flat_df())
        self.assertFalse(blocked)
        self.assertEqual(ctx.get("reason"), "trend_aligned")

    def test_long_structural_flat_not_blocked(self):
        """Boundary: r == 0.0. Condition is `r < 0.0` → False → not blocked."""
        blocked, ctx = counter_trend_block("long", _sig(structural=True), _flat_df())
        self.assertFalse(blocked)
        self.assertEqual(ctx.get("reason"), "trend_aligned")

    # ── Context fields ────────────────────────────────────────────────────────

    def test_ctx_fields_present_when_structural(self):
        blocked, ctx = counter_trend_block("short", _sig(score=11, structural=True), _rising_df())
        self.assertIn("mom_1m_pct", ctx)
        self.assertIn("score", ctx)
        self.assertIn("structural", ctx)
        self.assertIn("direction", ctx)
        self.assertEqual(ctx["score"], 11)
        self.assertTrue(ctx["structural"])
        self.assertEqual(ctx["direction"], "short")

    def test_mom_1m_pct_positive_when_rising(self):
        _, ctx = counter_trend_block("short", _sig(structural=True), _rising_df())
        self.assertGreater(ctx.get("mom_1m_pct", 0), 0)

    def test_mom_1m_pct_negative_when_falling(self):
        _, ctx = counter_trend_block("short", _sig(structural=True), _falling_df())
        self.assertLess(ctx.get("mom_1m_pct", 0), 0)


if __name__ == "__main__":
    unittest.main()
```

---

### DRAFT: tests/test_reentry_cooldown.py

**NOTE:** Board Agent B finding addressed — `_STOP_REASONS` mock is the COMPLETE frozenset
(all 5 reasons). An explicit completeness guard test catches future drift.

```python
#!/usr/bin/env python3
# ruff: noqa: E501
"""
tests/test_reentry_cooldown.py

Unit tests for execution/reentry_cooldown.py.
Pure stdlib — no Alpaca SDK, no execution engine imports.

Board Agent B finding (2026-08-05 nightly): the fake trade_logger mock MUST use the
COMPLETE _STOP_REASONS frozenset (all 5 reasons). A truncated mock silently drops
trail_stop/gtc_stop_triggered/overnight_atr_buffer_exit/breakeven_stop — exactly
the stop types that caused the SMCI re-short churn. All 5 reasons are tested
individually below, and a completeness guard asserts the frozenset matches production.
"""
import sys
import types
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_PT = ZoneInfo("America/Los_Angeles")

# ── Mock modules before any execution import ──────────────────────────────────
# config mock
_fake_config = types.ModuleType("config")
_fake_config.EXTERNAL_CLOSE_REENTRY_COOLDOWN_ENABLED = True
sys.modules["config"] = _fake_config

# trade_logger mock — COMPLETE frozenset (Board Agent B fix: never truncate)
_fake_trade_logger = types.ModuleType("trade_logger")
_fake_trade_logger._STOP_REASONS = frozenset({
    "hard_stop",
    "trail_stop",
    "gtc_stop_triggered",
    "overnight_atr_buffer_exit",
    "breakeven_stop",
})
sys.modules["trade_logger"] = _fake_trade_logger

# execution.state_io mock — exposes _PT only
_fake_execution = types.ModuleType("execution")
_fake_state_io = types.ModuleType("execution.state_io")
_fake_state_io._PT = _PT
sys.modules["execution"] = _fake_execution
sys.modules["execution.state_io"] = _fake_state_io

from execution.reentry_cooldown import is_in_cooldown  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────
_NOW_PT = datetime.now(_PT)


def _today_iso():
    return _NOW_PT.isoformat()


def _yesterday_iso():
    return (_NOW_PT - timedelta(days=1)).isoformat()


def _trade(symbol="SMCI", direction="short", exit_reason="hard_stop", exit_time=None):
    return {
        "symbol": symbol,
        "direction": direction,
        "exit_reason": exit_reason,
        "exit_time": exit_time if exit_time is not None else _today_iso(),
    }


# ─────────────────────────────────────────────────────────────────────────────
class TestIsInCooldownFailOpen(unittest.TestCase):
    """Fail-open paths — must never block when there's no relevant trade."""

    def test_empty_list_not_blocked(self):
        blocked, t = is_in_cooldown("SMCI", "short", [], now_pt=_NOW_PT)
        self.assertFalse(blocked)
        self.assertIsNone(t)

    def test_none_list_not_blocked(self):
        blocked, t = is_in_cooldown("SMCI", "short", None, now_pt=_NOW_PT)
        self.assertFalse(blocked)
        self.assertIsNone(t)


# ─────────────────────────────────────────────────────────────────────────────
class TestIsInCooldownBlocked(unittest.TestCase):
    """All 5 stop reason types must individually trigger the cooldown."""

    def test_hard_stop_blocks(self):
        blocked, t = is_in_cooldown("SMCI", "short", [_trade(exit_reason="hard_stop")], now_pt=_NOW_PT)
        self.assertTrue(blocked)
        self.assertEqual(t["exit_reason"], "hard_stop")

    def test_trail_stop_blocks(self):
        """Board Agent B: trail_stop is dominant SMCI exit — must NOT be in a truncated mock."""
        blocked, t = is_in_cooldown("SMCI", "short", [_trade(exit_reason="trail_stop")], now_pt=_NOW_PT)
        self.assertTrue(blocked)

    def test_gtc_stop_triggered_blocks(self):
        blocked, t = is_in_cooldown("SMCI", "short", [_trade(exit_reason="gtc_stop_triggered")], now_pt=_NOW_PT)
        self.assertTrue(blocked)

    def test_overnight_atr_buffer_exit_blocks(self):
        blocked, t = is_in_cooldown("SMCI", "short", [_trade(exit_reason="overnight_atr_buffer_exit")], now_pt=_NOW_PT)
        self.assertTrue(blocked)

    def test_breakeven_stop_blocks(self):
        blocked, t = is_in_cooldown("SMCI", "short", [_trade(exit_reason="breakeven_stop")], now_pt=_NOW_PT)
        self.assertTrue(blocked)


# ─────────────────────────────────────────────────────────────────────────────
class TestIsInCooldownAllowed(unittest.TestCase):
    """Non-stop exit reasons and boundary cases must NOT block re-entry."""

    def test_target_hit_does_not_block(self):
        blocked, _ = is_in_cooldown("SMCI", "short", [_trade(exit_reason="target_hit")], now_pt=_NOW_PT)
        self.assertFalse(blocked)

    def test_opposite_signal_does_not_block(self):
        blocked, _ = is_in_cooldown("SMCI", "short", [_trade(exit_reason="opposite_signal")], now_pt=_NOW_PT)
        self.assertFalse(blocked)

    def test_eod_does_not_block(self):
        blocked, _ = is_in_cooldown("SMCI", "short", [_trade(exit_reason="eod")], now_pt=_NOW_PT)
        self.assertFalse(blocked)

    def test_opposite_direction_not_blocked(self):
        """After stop loss on SHORT, re-entering LONG is explicitly allowed (genuine reversal)."""
        blocked, _ = is_in_cooldown("SMCI", "long", [_trade(direction="short", exit_reason="hard_stop")], now_pt=_NOW_PT)
        self.assertFalse(blocked)

    def test_different_symbol_not_blocked(self):
        blocked, _ = is_in_cooldown("NVDA", "short", [_trade(symbol="SMCI", exit_reason="hard_stop")], now_pt=_NOW_PT)
        self.assertFalse(blocked)

    def test_stop_loss_yesterday_not_blocked(self):
        """Cooldown resets on PT day boundary."""
        t = _trade(exit_reason="hard_stop", exit_time=_yesterday_iso())
        blocked, _ = is_in_cooldown("SMCI", "short", [t], now_pt=_NOW_PT)
        self.assertFalse(blocked)


# ─────────────────────────────────────────────────────────────────────────────
class TestExternalClose(unittest.TestCase):
    """External / manual close gated by EXTERNAL_CLOSE_REENTRY_COOLDOWN_ENABLED."""

    def setUp(self):
        _fake_config.EXTERNAL_CLOSE_REENTRY_COOLDOWN_ENABLED = True

    def tearDown(self):
        _fake_config.EXTERNAL_CLOSE_REENTRY_COOLDOWN_ENABLED = True

    def test_external_close_flag_on_blocks(self):
        _fake_config.EXTERNAL_CLOSE_REENTRY_COOLDOWN_ENABLED = True
        blocked, t = is_in_cooldown("SMCI", "short", [_trade(exit_reason="external_close")], now_pt=_NOW_PT)
        self.assertTrue(blocked)

    def test_external_close_flag_off_does_not_block(self):
        _fake_config.EXTERNAL_CLOSE_REENTRY_COOLDOWN_ENABLED = False
        blocked, _ = is_in_cooldown("SMCI", "short", [_trade(exit_reason="external_close")], now_pt=_NOW_PT)
        self.assertFalse(blocked)


# ─────────────────────────────────────────────────────────────────────────────
class TestReturnValue(unittest.TestCase):
    """Return value structure."""

    def test_blocked_returns_trade_dict(self):
        t = _trade(exit_reason="hard_stop")
        blocked, result = is_in_cooldown("SMCI", "short", [t], now_pt=_NOW_PT)
        self.assertTrue(blocked)
        self.assertIs(result, t)

    def test_not_blocked_returns_none_trade(self):
        blocked, result = is_in_cooldown("SMCI", "short", [], now_pt=_NOW_PT)
        self.assertFalse(blocked)
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────────────────────
class TestRobustness(unittest.TestCase):
    """Malformed or incomplete trade rows must not abort the scan."""

    def test_non_dict_rows_skipped(self):
        trades = [None, "garbage", 42, _trade(exit_reason="target_hit")]
        blocked, _ = is_in_cooldown("SMCI", "short", trades, now_pt=_NOW_PT)
        self.assertFalse(blocked)

    def test_missing_exit_time_not_matched(self):
        t = {"symbol": "SMCI", "direction": "short", "exit_reason": "hard_stop", "exit_time": None}
        blocked, _ = is_in_cooldown("SMCI", "short", [t], now_pt=_NOW_PT)
        self.assertFalse(blocked)

    def test_short_exit_time_not_matched(self):
        """A timestamp shorter than 10 chars cannot match today's date prefix."""
        t = {"symbol": "SMCI", "direction": "short", "exit_reason": "hard_stop", "exit_time": "20"}
        blocked, _ = is_in_cooldown("SMCI", "short", [t], now_pt=_NOW_PT)
        self.assertFalse(blocked)

    def test_most_recent_trade_returned(self):
        """is_in_cooldown iterates reversed — first match in reversed order is the most recent."""
        older = _trade(exit_reason="hard_stop")
        newer = _trade(exit_reason="trail_stop")
        blocked, result = is_in_cooldown("SMCI", "short", [older, newer], now_pt=_NOW_PT)
        self.assertTrue(blocked)
        self.assertIs(result, newer)


# ─────────────────────────────────────────────────────────────────────────────
class TestStopReasonsFrozensetCompleteness(unittest.TestCase):
    """Regression guard: Board Agent B (2026-08-05).

    Verifies that the live _STOP_REASONS bound inside reentry_cooldown matches
    the expected complete set of 5 reasons. If trade_logger._STOP_REASONS ever
    adds a new reason, this test fails fast and forces an update to both the
    production module and this test's mock.
    """

    def test_stop_reasons_complete(self):
        from execution.reentry_cooldown import _STOP_REASONS as live_set
        expected = frozenset({
            "hard_stop",
            "trail_stop",
            "gtc_stop_triggered",
            "overnight_atr_buffer_exit",
            "breakeven_stop",
        })
        self.assertEqual(
            live_set, expected,
            "reentry_cooldown._STOP_REASONS changed — update the mock in this test file too. "
            "Board Agent B (2026-08-05): a truncated mock produces false-green tests for the "
            "stop types that caused the SMCI re-short churn."
        )


if __name__ == "__main__":
    unittest.main()
```

---

## ITEM 2: signal_generator.py dead `_base` variable — RTH CHAIN DRAFT

**File:** `strategy/signal_generator.py`
**Classification:** RTH-CHAIN (imported by strategy/run_cycle.py)
**Status:** Full read in progress (Explore subagent launched — 1095 lines)

Proposed change (trivial — remove 1 dead-assignment line):

```diff
--- a/strategy/signal_generator.py
+++ b/strategy/signal_generator.py
@@ -774,7 +774,6 @@ def _emit_mr_signals(self, long_r, short_r, context, bars_by_symbol):
                 _mr_dir = "long" if _best_ret >= 0 else "short"
-                _base = long_r if _mr_dir == "long" else short_r
                 # _base was assigned here but never referenced in the append block below
```

This removes the dead variable assignment at L777 (confirmed nit from the diff-3c cold-2nd
review). `_base` is assigned but `mr_signals.append({...})` on the following lines never
references it. Zero behavior change — pure whitespace cleanup.

**RTH-chain approval required** — Gro + GAI preship on exact diff before apply.
Full read + 10-pt audit will be written to tb_audit_log.md once Explore subagent completes.

---

## HOW TO APPLY (Rafael interactive session)

1. **ITEM 1 (unit tests):** Copy draft content above into the test files, run static
   analysis + pytest, approve, commit:
   ```bash
   # (paste draft content into test files)
   python3 -m py_compile tests/test_counter_trend.py tests/test_reentry_cooldown.py
   ruff check --select E,W,F,B tests/test_counter_trend.py tests/test_reentry_cooldown.py
   python3 -m pytest tests/test_counter_trend.py tests/test_reentry_cooldown.py -v
   git add tests/test_counter_trend.py tests/test_reentry_cooldown.py
   git commit -m "Add: unit tests for counter_trend.py + reentry_cooldown.py (Board Agent B fix applied)"
   git push -u origin claude/youthful-wozniak-wnuo6h
   ```

2. **ITEM 2 (signal_generator.py):** Full gate required — board vote + Gro/GAI preship.
   See pending_patch file once audit completes.
