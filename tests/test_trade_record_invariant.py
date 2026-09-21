#!/usr/bin/env python3
# ruff: noqa: E501  — synthetic event dicts + assertions run long (matches tests/ convention)
"""
tests/test_trade_record_invariant.py — the edge-discovery Step 1 enforcing tests.
Design: logs/design_records/edge_discovery_2026-09-19.md (BGGN 5/5).

Guards the measurement contract:
  (A) Every required record FIELD is present + JSON-serializable — feeding a numpy.bool_ (the exact
      D1 2026-07-20..27 regression: a numpy.bool_ silently dropped EVERY entry for 7 days).
  (B) R-multiple math (planned_R, realized_R, MAE/MFE) is correct for both long and short.
  (C) Reducer reconciliation: every emitted row has a matched entry; an exit with no entry is
      counted as an orphan and NOT emitted (would have caught the 7-day drop); a P&L correction
      rewrites the loss (never-mask-a-loss).
"""
import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402  research engines already depend on numpy

from trade_logger import (  # noqa: E402
    ENTRY_NONNULL_FIELDS,
    REQUIRED_ENTRY_FIELDS,
    REQUIRED_EXIT_FIELDS,
    _json_default,
    compute_mae_mfe_R,
    compute_planned_R,
    compute_realized_R,
    make_components,
    make_entry_record,
    make_exit_record,
    make_trade_record,
    outcome_from_reason,
)
from research import trade_record_reducer as R  # noqa: E402


def _full_entry(**over):
    kw = dict(
        trade_id="INTRA-AAPL-t0", tier="intraday", symbol="AAPL", side="long",
        entry_price=100.0, stop_price=95.0, target_price=110.0, qty=3,
        score_raw=9, score_max=12,
        components=make_components({"daily_above_150sma": True, "ema13_above_ema30": True}),
        indicators={"tsmom_12m": 12.3},
    )
    kw.update(over)
    return make_entry_record(**kw)


def _full_exit(**over):
    kw = dict(
        trade_id="INTRA-AAPL-t0", tier="intraday", symbol="AAPL", side="long",
        entry_price=100.0, stop_price=95.0, exit_price=110.0, qty=3,
        realized_pnl=30.0, exit_reason="take_profit", min_price=98.0, max_price=112.0,
    )
    kw.update(over)
    return make_exit_record(**kw)


class RequiredFields(unittest.TestCase):
    def test_entry_has_all_required_fields(self):
        rec = _full_entry()
        missing = REQUIRED_ENTRY_FIELDS - set(rec)
        self.assertFalse(missing, f"entry record missing required fields: {missing}")

    def test_entry_nonnull_core_fields(self):
        rec = _full_entry()
        for f in ENTRY_NONNULL_FIELDS:
            self.assertIsNotNone(rec[f], f"entry field {f} must be non-null")

    def test_exit_has_all_required_fields(self):
        rec = _full_exit()
        missing = REQUIRED_EXIT_FIELDS - set(rec)
        self.assertFalse(missing, f"exit record missing required fields: {missing}")

    def test_merged_record_covers_both(self):
        row = make_trade_record(_full_entry(), _full_exit())
        missing = (REQUIRED_ENTRY_FIELDS | REQUIRED_EXIT_FIELDS) - set(row)
        self.assertFalse(missing, f"merged record missing: {missing}")


class D1SerializationGuard(unittest.TestCase):
    """The record must survive json.dumps(default=_json_default) even with numpy scalars in it —
    the exact class of bug that silently dropped every entry for 7 days."""

    def test_numpy_bool_in_components_serializes(self):
        conds = {"daily_above_150sma": np.bool_(True), "ema13_above_ema30": np.bool_(False)}
        rec = _full_entry(components=make_components(conds),
                          indicators={"tsmom_12m": np.float64(12.34)})
        s = json.dumps(rec, default=_json_default)  # must NOT raise
        back = json.loads(s)
        self.assertEqual(back["components"]["daily_above_150sma"]["value"], True)
        self.assertEqual(back["components"]["daily_above_150sma"]["contribution"], 2)

    def test_numpy_in_exit_serializes(self):
        rec = _full_exit(realized_pnl=np.float64(-3.09), exit_price=np.float64(96.9))
        json.dumps(rec, default=_json_default)  # must NOT raise


class ConfidenceLayer0(unittest.TestCase):
    def test_defaults_to_score_ratio(self):
        rec = _full_entry(confidence=None, score_raw=9, score_max=12)
        self.assertAlmostEqual(rec["confidence"], 0.75, places=4)

    def test_explicit_conviction_used_verbatim(self):
        rec = _full_entry(confidence=0.66, score_raw=None, score_max=1.0)
        self.assertAlmostEqual(rec["confidence"], 0.66, places=4)

    def test_confidence_clamped_0_1(self):
        rec = _full_entry(confidence=None, score_raw=13, score_max=12)
        self.assertLessEqual(rec["confidence"], 1.0)


class RMath(unittest.TestCase):
    def test_planned_R_long(self):
        self.assertAlmostEqual(compute_planned_R(100, 95, 110), 2.0, places=4)  # reward10/risk5

    def test_realized_R_long_win(self):
        self.assertAlmostEqual(compute_realized_R(100, 95, 110, "long"), 2.0, places=4)

    def test_realized_R_long_loss(self):
        self.assertAlmostEqual(compute_realized_R(100, 95, 95, "long"), -1.0, places=4)

    def test_realized_R_short_win(self):
        # short entry 100 stop 105 (risk5) exit 90 -> reward10 -> +2R
        self.assertAlmostEqual(compute_realized_R(100, 105, 90, "short"), 2.0, places=4)

    def test_realized_R_zero_risk_is_none(self):
        self.assertIsNone(compute_realized_R(100, 100, 110, "long"))

    def test_mae_mfe_long(self):
        # entry100 stop95 risk5, low98 high107 -> mae=(100-98)/5=0.4, mfe=(107-100)/5=1.4
        mae, mfe = compute_mae_mfe_R(100, 95, 98, 107, "long")
        self.assertAlmostEqual(mae, 0.4, places=4)
        self.assertAlmostEqual(mfe, 1.4, places=4)

    def test_mae_mfe_short(self):
        # short entry100 stop105 risk5, low92 high103 -> fav=low: mfe=(100-92)/5=1.6, mae=(103-100)/5=0.6
        mae, mfe = compute_mae_mfe_R(100, 105, 92, 103, "short")
        self.assertAlmostEqual(mae, 0.6, places=4)
        self.assertAlmostEqual(mfe, 1.6, places=4)

    def test_excursions_nonnegative(self):
        mae, mfe = compute_mae_mfe_R(100, 95, 100, 100, "long")  # never moved
        self.assertGreaterEqual(mae, 0.0)
        self.assertGreaterEqual(mfe, 0.0)


class OutcomeLabel(unittest.TestCase):
    def test_target_is_plus_one(self):
        self.assertEqual(outcome_from_reason("take_profit"), 1)
        self.assertEqual(outcome_from_reason("target hit"), 1)

    def test_stop_is_minus_one(self):
        self.assertEqual(outcome_from_reason("hard_stop"), -1)
        self.assertEqual(outcome_from_reason("overnight_atr_buffer_exit | 9-scan breach"), -1)

    def test_time_external_is_zero(self):
        self.assertEqual(outcome_from_reason("eod_flatten"), 0)
        self.assertEqual(outcome_from_reason("external_close"), 0)

    def test_label_independent_of_pnl_sign(self):
        # a stop-out that happened to be tiny-positive is still label -1 (barrier, not P&L)
        self.assertEqual(outcome_from_reason("trail_stop"), -1)


class ReducerIntraday(unittest.TestCase):
    def _events(self):
        return [
            {"ts": "2026-09-01T07:00:00-07:00", "event": "entry", "symbol": "AAPL",
             "price": 100.0, "size": 3, "score": 9, "mri_level": "NORMAL", "direction": "long",
             "stop": 95.0, "target": 110.0,
             "conditions": {"daily_above_150sma": True, "ema13_above_ema30": False}},
            {"ts": "2026-09-01T09:00:00-07:00", "event": "exit", "symbol": "AAPL",
             "price": 110.0, "size": 3, "pnl": 30.0, "reason": "take_profit", "direction": "long"},
        ]

    def test_one_closed_record(self):
        recs, stats = R.reduce_intraday(self._events())
        self.assertEqual(len(recs), 1)
        self.assertEqual(stats["closed"], 1)
        self.assertEqual(stats["orphan_exits"], 0)
        row = recs[0]
        self.assertEqual(row["tier"], "intraday")
        self.assertAlmostEqual(row["realized_R"], 2.0, places=4)
        self.assertEqual(row["outcome_label"], 1)
        self.assertAlmostEqual(row["confidence"], 0.75, places=4)
        self.assertEqual(row["components"]["daily_above_150sma"]["contribution"], 2)

    def test_reduce_output_serializes(self):
        recs, _ = R.reduce_intraday(self._events())
        json.dumps(recs)  # plain dumps must succeed (all values JSON-native)

    def test_fifo_two_lots_same_symbol(self):
        evs = [
            {"ts": "2026-09-01T07:00:00-07:00", "event": "entry", "symbol": "AAPL",
             "price": 100.0, "size": 1, "score": 9, "direction": "long", "stop": 95.0, "target": 110.0},
            {"ts": "2026-09-01T07:05:00-07:00", "event": "entry", "symbol": "AAPL",
             "price": 200.0, "size": 1, "score": 10, "direction": "long", "stop": 190.0, "target": 220.0},
            {"ts": "2026-09-01T08:00:00-07:00", "event": "exit", "symbol": "AAPL",
             "price": 110.0, "size": 1, "pnl": 10.0, "reason": "take_profit", "direction": "long"},
            {"ts": "2026-09-01T09:00:00-07:00", "event": "stop_hit", "symbol": "AAPL",
             "price": 190.0, "size": 1, "pnl": -10.0, "reason": "hard_stop", "direction": "long"},
        ]
        recs, stats = R.reduce_intraday(evs)
        self.assertEqual(stats["closed"], 2)
        # FIFO: first exit (110) closes the 100-entry lot; the stop closes the 200-entry lot
        self.assertAlmostEqual(recs[0]["entry_price"], 100.0)
        self.assertEqual(recs[0]["outcome_label"], 1)
        self.assertAlmostEqual(recs[1]["entry_price"], 200.0)
        self.assertEqual(recs[1]["outcome_label"], -1)

    def test_orphan_exit_counted_not_emitted(self):
        evs = [{"ts": "2026-09-01T09:00:00-07:00", "event": "exit", "symbol": "ZZZ",
                "price": 50.0, "size": 1, "pnl": -1.0, "reason": "external_close", "direction": "long"}]
        recs, stats = R.reduce_intraday(evs)
        self.assertEqual(len(recs), 0)
        self.assertEqual(stats["orphan_exits"], 1)

    def test_pnl_correction_rewrites_loss(self):
        # No original_pnl on the correction -> overwrite fallback (realized becomes corrected).
        evs = self._events() + [
            {"ts": "2026-09-01T09:00:10-07:00", "event": "exit_pnl_correction", "symbol": "AAPL",
             "corrected_exit_price": 96.0, "corrected_pnl": -12.0}]
        recs, stats = R.reduce_intraday(evs)
        self.assertEqual(stats["corrections"], 1)
        self.assertAlmostEqual(recs[0]["realized_pnl"], -12.0, places=2)  # never-mask-a-loss
        self.assertTrue(recs[0]["pnl_corrected"])

    def test_intraday_partial_not_double_counted(self):
        # PRODUCTION semantics (portfolio_tracker.record_exit): the FINAL exit's `pnl` is ALREADY
        # the trade TOTAL (remaining leg + all partial tranches). entry 2sh; partial_exit pnl=+5
        # (leg); stop_hit pnl=-1 (TOTAL = remaining -6 + partial +5). Reducer must record -1, NOT
        # +4 (re-adding the partial would flip a losing trade to a gain — the Finding-1 bug).
        evs = [
            {"ts": "t1", "event": "entry", "symbol": "AAPL", "price": 100.0, "size": 2,
             "score": 9, "direction": "long", "stop": 95.0, "target": 110.0},
            {"ts": "t2", "event": "partial_exit", "symbol": "AAPL", "price": 105.0, "size": 1, "pnl": 5.0},
            {"ts": "t3", "event": "stop_hit", "symbol": "AAPL", "price": 94.0, "size": 1, "pnl": -1.0,
             "reason": "hard_stop", "direction": "long"},
        ]
        recs, stats = R.reduce_intraday(evs)
        self.assertEqual(stats["partials"], 1)
        self.assertEqual(len(recs), 1)
        self.assertAlmostEqual(recs[0]["realized_pnl"], -1.0, places=2)  # NOT +4
        self.assertEqual(recs[0]["outcome_label"], -1)
        self.assertAlmostEqual(recs[0]["qty"], 2.0)  # ORIGINAL entry qty, not post-partial remaining

    def test_correction_on_total_basis(self):
        # correction original_pnl/corrected_pnl are TOTAL-basis (portfolio_tracker _total_pnl).
        # base total = -1; delta = -3-(-1) = -2; row = -3.
        evs = [
            {"ts": "t1", "event": "entry", "symbol": "AAPL", "price": 100.0, "size": 2,
             "score": 9, "direction": "long", "stop": 95.0, "target": 110.0},
            {"ts": "t2", "event": "partial_exit", "symbol": "AAPL", "price": 105.0, "size": 1, "pnl": 5.0},
            {"ts": "t3", "event": "stop_hit", "symbol": "AAPL", "price": 94.0, "size": 1, "pnl": -1.0,
             "reason": "hard_stop", "direction": "long"},
            {"ts": "t4", "event": "exit_pnl_correction", "symbol": "AAPL",
             "original_pnl": -1.0, "corrected_pnl": -3.0, "corrected_exit_price": 92.0},
        ]
        recs, stats = R.reduce_intraday(evs)
        self.assertEqual(stats["corrections"], 1)
        self.assertAlmostEqual(recs[0]["realized_pnl"], -3.0, places=2)

    def test_daytrade_rows_skipped_from_intraday(self):
        # Finding 2: day-tier trades are dual-written to the SHARED trade_events.jsonl with
        # data_source/tier="daytrade" and realized_pnl=/exit_reason= (not pnl=/reason=). If
        # reduce_intraday ingested them, the loss would mask to $0 and inflate intraday N.
        evs = [
            {"ts": "t1", "event": "entry", "symbol": "MSFT", "price": 499.0, "size": 1,
             "data_source": "daytrade", "tier": "daytrade", "direction": "short",
             "stop": 504.0, "trade_id": "DT-x"},
            {"ts": "t2", "event": "exit", "symbol": "MSFT", "price": 504.0, "size": 1,
             "data_source": "daytrade", "tier": "daytrade", "realized_pnl": -5.0,
             "exit_reason": "protective_stop", "trade_id": "DT-x"},
        ]
        recs, stats = R.reduce_intraday(evs)
        self.assertEqual(len(recs), 0)
        self.assertEqual(stats["closed"], 0)
        self.assertGreaterEqual(stats["daytrade_skipped"], 2)

    def test_orphan_exit_outcome_distribution(self):
        evs = [{"ts": "t1", "event": "stop_hit", "symbol": "ZZZ", "price": 50.0, "size": 1,
                "pnl": -3.0, "reason": "hard_stop", "direction": "long"}]
        recs, stats = R.reduce_intraday(evs)
        self.assertEqual(len(recs), 0)
        self.assertEqual(stats["orphan_exits"], 1)
        self.assertEqual(stats["orphan_losses"], 1)
        self.assertAlmostEqual(stats["orphan_pnl_sum"], -3.0, places=2)


class ReducerDayTier(unittest.TestCase):
    def _events(self):
        did = "DT-MSFT-dec"
        tid = "DT-MSFT-fill"
        return [
            {"ts": "2026-09-15T08:50:12-07:00", "event": "decision", "trade_id": did,
             "decision_id": did, "symbol": "MSFT",
             "decision": {"side": "SHORT", "conviction": 0.66, "gex_action": "FADE",
                          "gex_label": "POSITIVE", "strength": 0.43, "side_score": 1.0},
             "trigger": {"mode": "FADE", "vol_confirmed": True, "entry_ref": 499.4, "wall_ref": 500.0}},
            {"ts": "2026-09-15T08:50:13-07:00", "event": "entry_fill", "trade_id": tid,
             "symbol": "MSFT", "order_id": "o1", "decision_id": did, "side": "short",
             "fill_price": 499.25, "fill_qty": 1.0, "notional": 499.25},
            {"ts": "2026-09-15T08:50:13-07:00", "event": "stop_placed", "trade_id": tid,
             "symbol": "MSFT", "stop_order_id": "s1", "stop_price": 504.25},  # risk = 5.0
            {"ts": "2026-09-15T09:00:00-07:00", "event": "price_sample", "trade_id": tid,
             "symbol": "MSFT", "seq": 1, "market_price": 497.25, "unrealized_pnl": 2.0},
            {"ts": "2026-09-15T09:30:00-07:00", "event": "price_sample", "trade_id": tid,
             "symbol": "MSFT", "seq": 2, "market_price": 501.75, "unrealized_pnl": -2.5},
            {"ts": "2026-09-15T10:00:00-07:00", "event": "exit_fill", "trade_id": tid,
             "symbol": "MSFT", "order_id": "o2", "exit_reason": "protective_stop",
             "fill_price": 504.25, "fill_qty": 1.0, "market_price_at_exit": 504.25,
             "realized_pnl": -5.0}]

    def test_one_closed_daytrade(self):
        recs, stats = R.reduce_day_tier(self._events())
        self.assertEqual(stats["closed"], 1)
        self.assertEqual(stats["decision_joined"], 1)
        self.assertEqual(stats["open"], 0)
        row = recs[0]
        self.assertEqual(row["tier"], "daytrade")
        self.assertEqual(row["side"], "short")
        self.assertAlmostEqual(row["confidence"], 0.66, places=4)
        self.assertEqual(row["gex_regime"], "POSITIVE")
        self.assertAlmostEqual(row["realized_pnl"], -5.0, places=2)
        self.assertEqual(row["outcome_label"], -1)
        # short: entry 499.25, stop 504.25 (risk 5). exit 504.25 -> realized_R = -1.0
        self.assertAlmostEqual(row["realized_R"], -1.0, places=3)
        # MAE/MFE from samples 497.25/501.75: short fav=low -> mfe=(499.25-497.25)/5=0.4
        # mae=(501.75-499.25)/5=0.5
        self.assertAlmostEqual(row["mfe_R"], 0.4, places=3)
        self.assertAlmostEqual(row["mae_R"], 0.5, places=3)

    def test_open_trade_not_emitted(self):
        evs = [e for e in self._events() if e["event"] != "exit_fill"]
        recs, stats = R.reduce_day_tier(evs)
        self.assertEqual(len(recs), 0)
        self.assertEqual(stats["open"], 1)
        self.assertEqual(stats["closed"], 0)

    def test_output_serializes(self):
        recs, _ = R.reduce_day_tier(self._events())
        json.dumps(recs)


class ReducerReconciliation(unittest.TestCase):
    """Design invariant (B): emitted rows == matched entries; an exit with no entry is an orphan."""

    def test_intraday_closed_plus_orphans_accounts_for_all_exits(self):
        evs = [
            {"ts": "t1", "event": "entry", "symbol": "A", "price": 10, "size": 1,
             "direction": "long", "stop": 9, "target": 12, "score": 9},
            {"ts": "t2", "event": "exit", "symbol": "A", "price": 12, "size": 1, "pnl": 2,
             "reason": "take_profit", "direction": "long"},
            {"ts": "t3", "event": "exit", "symbol": "B", "price": 5, "size": 1, "pnl": -1,
             "reason": "external_close", "direction": "long"},  # orphan
        ]
        recs, stats = R.reduce_intraday(evs)
        total_exits = 2
        self.assertEqual(stats["closed"] + stats["orphan_exits"], total_exits)
        self.assertEqual(len(recs), stats["closed"])


class LiveEmitTradeId(unittest.TestCase):
    """Inc 2 (Piece 1a) enforcement: the LIVE intraday emit path stamps a matching trade_id on
    the entry AND the exit event. Its absence is the exact class that let the 7-day entry-drop
    hide (no join key => a dropped entry looked like an orphan exit, silently). Drives the REAL
    PortfolioTracker.record_entry -> record_exit with a mocked logger + an isolated trade_log."""

    def _drive(self, exit_reason):
        import tempfile
        from unittest import mock
        import execution.portfolio_tracker as PT
        captured = []
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(PT, "TRADE_LOG_FILE", Path(d) / "trade_log.json"), \
                 mock.patch.object(PT, "_log_event",
                                   lambda event, **kw: captured.append({"event": event, **kw})):
                tr = PT.PortfolioTracker()
                tr.record_entry("AAPL", "long", 3, 100.0, 95.0, 110.0, "intraday", 9)
                tr.record_exit("AAPL", 110.0, reason=exit_reason)
        return captured

    def test_entry_and_exit_share_a_nonempty_trade_id(self):
        captured = self._drive("take_profit")
        entries = [e for e in captured if e["event"] == "entry"]
        exits   = [e for e in captured if e["event"] in ("exit", "stop_hit")]
        self.assertEqual(len(entries), 1, "exactly one entry event must be emitted")
        self.assertEqual(len(exits), 1, "exactly one exit event must be emitted")
        self.assertTrue(entries[0].get("trade_id"), "entry event must carry a non-empty trade_id")
        self.assertEqual(entries[0]["trade_id"], exits[0]["trade_id"],
                         "exit event trade_id must match the entry event's")
        self.assertTrue(entries[0]["trade_id"].startswith("INTRA-AAPL-"))

    def test_stop_exit_also_carries_the_trade_id(self):
        # a stop-reason exit routes to the stop_hit event type; it must still carry the id
        captured = self._drive("hard_stop")
        exits = [e for e in captured if e["event"] == "stop_hit"]
        self.assertEqual(len(exits), 1)
        self.assertTrue(exits[0].get("trade_id"))


class ReducerUsesRealTradeId(unittest.TestCase):
    """Inc 2 (Piece 1a): the reducer records the REAL minted trade_id when the event carries one,
    and falls back to the synthesized symbol+ts id for legacy rows (unchanged FIFO pairing)."""

    def test_real_trade_id_preferred(self):
        evs = [
            {"ts": "2026-09-01T07:00:00-07:00", "event": "entry", "symbol": "AAPL",
             "price": 100.0, "size": 1, "score": 9, "direction": "long", "stop": 95.0,
             "target": 110.0, "trade_id": "INTRA-AAPL-2026-09-01T07:00:00.123456-07:00"},
            {"ts": "2026-09-01T08:00:00-07:00", "event": "exit", "symbol": "AAPL",
             "price": 110.0, "size": 1, "pnl": 10.0, "reason": "take_profit", "direction": "long",
             "trade_id": "INTRA-AAPL-2026-09-01T07:00:00.123456-07:00"},
        ]
        recs, _ = R.reduce_intraday(evs)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["trade_id"], "INTRA-AAPL-2026-09-01T07:00:00.123456-07:00")

    def test_legacy_rows_fall_back_to_synthesized(self):
        evs = [
            {"ts": "2026-09-01T07:00:00-07:00", "event": "entry", "symbol": "AAPL",
             "price": 100.0, "size": 1, "score": 9, "direction": "long", "stop": 95.0, "target": 110.0},
            {"ts": "2026-09-01T08:00:00-07:00", "event": "exit", "symbol": "AAPL",
             "price": 110.0, "size": 1, "pnl": 10.0, "reason": "take_profit", "direction": "long"},
        ]
        recs, _ = R.reduce_intraday(evs)
        self.assertEqual(recs[0]["trade_id"], "INTRA-AAPL-2026-09-01T07:00:00-07:00")


class LiveEmitRegime(unittest.TestCase):
    """Inc 2 Piece 1b: the swing-tier entry event carries the daily regime label + detail + ts,
    and the reader helper fail-safes to UNKNOWN on a missing/malformed ledger (never raises on the
    trading thread; never fresh-washes a stale/absent regime)."""

    def test_entry_event_carries_regime(self):
        import tempfile
        from unittest import mock
        import execution.portfolio_tracker as PT
        captured = []
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(PT, "TRADE_LOG_FILE", Path(d) / "trade_log.json"), \
                 mock.patch.object(PT, "_log_event",
                                   lambda event, **kw: captured.append({"event": event, **kw})), \
                 mock.patch.object(PT, "_read_regime_snapshot",
                                   lambda: ("BULL", {"vol_composite": "BULL", "any_stale": True},
                                            "2026-09-18T13:12:04-07:00", 123.4)):
                tr = PT.PortfolioTracker()
                tr.record_entry("AAPL", "long", 3, 100.0, 95.0, 110.0, "intraday", 9)
        entry = [e for e in captured if e["event"] == "entry"][0]
        self.assertEqual(entry["regime"], "BULL")
        self.assertEqual(entry["regime_detail"]["vol_composite"], "BULL")
        self.assertEqual(entry["regime_ts"], "2026-09-18T13:12:04-07:00")
        self.assertEqual(entry["regime_age_sec"], 123.4)

    def test_regime_snapshot_failsafe_unknown_on_missing(self):
        import tempfile
        from unittest import mock
        import execution.portfolio_tracker as PT
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(PT, "_REGIME_LEDGER", Path(d) / "nope.json"):
                label, detail, ts, age = PT._read_regime_snapshot()
        self.assertEqual(label, "UNKNOWN")
        self.assertEqual(detail, {})
        self.assertIsNone(ts)
        self.assertIsNone(age)

    def test_regime_snapshot_reads_real_ledger(self):
        import json as _j
        import tempfile
        from datetime import datetime as _dt, timedelta as _timedelta, timezone as _tz
        from unittest import mock
        import execution.portfolio_tracker as PT
        # Deterministic regardless of wall clock: a ledger stamped 2 days BEFORE now must read
        # back as ~2 days old (fresh-wash guard — a stale ledger is visibly old, never age 0).
        _ts = (_dt.now(_tz.utc) - _timedelta(days=2)).isoformat()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "regime_state.json"
            p.write_text(_j.dumps({"ts": _ts,
                                   "summary": {"vol_composite": "BULL", "vol_regime": "normal",
                                               "any_stale": False}}))
            with mock.patch.object(PT, "_REGIME_LEDGER", p):
                label, detail, ts, age = PT._read_regime_snapshot()
        self.assertEqual(label, "BULL")
        self.assertEqual(detail.get("vol_composite"), "BULL")
        self.assertEqual(ts, _ts)
        self.assertIsNotNone(age)
        self.assertGreater(age, 1.9 * 86400)   # visibly ~2 days old
        self.assertLess(age, 2.1 * 86400)

    def test_regime_age_helper_never_crashes(self):
        # Devil's-advocate hardening: the age helper must never raise on the trading thread and
        # must fail-safe to None on anything unparseable (naive ts, Z-suffix, garbage, None,
        # non-string). A parseable ts returns a non-negative float. Verified on OCI py3.10.
        import execution.portfolio_tracker as PT
        # (1) NEVER raises for ANY input — the only hard guarantee (result is None or float).
        #     "...Z" is version-dependent (py3.10 -> None, py3.11+ -> float) so it is only checked
        #     here for no-crash, never asserted to a specific value.
        for anything in ("garbage!!!", "", None, 12345, "2026-13-99T99:99:99",
                         "2026-09-18T16:12:00Z", "2026-09-18T16:12:00", [], {}):
            r = PT._regime_age_seconds(anything)
            self.assertTrue(r is None or isinstance(r, float), f"bad return for {anything!r}: {r!r}")
        # (2) Definitely-unparseable inputs fail-safe to None.
        for bad in ("garbage!!!", "", None, 12345, "2026-13-99T99:99:99"):
            self.assertIsNone(PT._regime_age_seconds(bad), f"expected None for {bad!r}")
        # (3) Parseable timestamps return a non-negative float — no crash. Uses the REAL ledger
        #     format (6-digit microseconds + offset, as regime_state.py writes via isoformat()) and
        #     plain no-fraction forms. NOT a 1-digit fraction: py3.10 (the OCI deploy target)
        #     rejects "..04.1" while py3.11+ accepts it — a 1-digit fraction is not a real input and
        #     the code correctly fail-safes it to None, so it belongs only in the no-crash loop above.
        for ok in ("2026-09-18T16:12:00", "2026-09-18 16:12:00", "2026-09-18T13:12:04.116843-07:00"):
            v = PT._regime_age_seconds(ok)
            self.assertIsInstance(v, float, f"expected float for {ok!r}")
            self.assertGreaterEqual(v, 0.0)

    def test_regime_snapshot_malformed_is_unknown(self):
        import tempfile
        from unittest import mock
        import execution.portfolio_tracker as PT
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "regime_state.json"
            p.write_text("not json {{{")
            with mock.patch.object(PT, "_REGIME_LEDGER", p):
                label, detail, ts, age = PT._read_regime_snapshot()
        self.assertEqual(label, "UNKNOWN")

    def test_read_snapshot_extracts_raw_signals_full_ledger(self):
        # F1/F4 CONTRACT: this fixture mirrors strategy.regime_state.compute_regime_state() output
        # (regime_state.py compute_regime_state -> {ts, vol{...}, macro{...}, market_mr{...},
        # summary{...}}). If that schema is renamed, update BOTH _read_regime_snapshot AND this
        # fixture. Asserts the reader lifts the RAW numeric signals (not just labels) so the record
        # is dynamic-recalibration-ready without a sidecar join.
        import json as _j
        import tempfile
        from unittest import mock
        import execution.portfolio_tracker as PT
        state = {
            "ts": "2026-09-18T13:12:04.116843-07:00",
            "vol": {"fresh": True, "regime": "normal", "realized_vol": 14.2,
                    "composite": "BULL", "vix_term_ratio": 0.93, "spy_vs_50sma_pct": 2.1},
            "macro": {"fresh": False, "label": "UNKNOWN", "composite_score": None, "confidence": None},
            "market_mr": {"fresh": True, "mean_reverting": True, "variance_ratio": 0.78, "hurst": 0.41},
            "summary": {"vol_regime": "normal", "vol_composite": "BULL", "macro_label": "UNKNOWN",
                        "market_mean_reverting": True, "any_stale": True},
        }
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "regime_state.json"
            p.write_text(_j.dumps(state))
            with mock.patch.object(PT, "_REGIME_LEDGER", p):
                label, detail, ts, age = PT._read_regime_snapshot()
        self.assertEqual(label, "BULL")
        # summary labels preserved
        self.assertEqual(detail["vol_composite"], "BULL")
        self.assertTrue(detail["any_stale"])
        # RAW signals lifted from the component dicts (the F1 point)
        self.assertEqual(detail["realized_vol"], 14.2)
        self.assertEqual(detail["vix_term_ratio"], 0.93)
        self.assertEqual(detail["variance_ratio"], 0.78)
        self.assertEqual(detail["hurst"], 0.41)
        # per-component freshness (the F3 write-time-staleness signal)
        self.assertTrue(detail["vol_fresh"])
        self.assertFalse(detail["macro_fresh"])
        self.assertTrue(detail["mr_fresh"])


class ReducerCapturesRegime(unittest.TestCase):
    def test_regime_on_record(self):
        evs = [
            {"ts": "2026-09-01T07:00:00-07:00", "event": "entry", "symbol": "AAPL",
             "price": 100.0, "size": 1, "score": 9, "direction": "long", "stop": 95.0,
             "target": 110.0, "trade_id": "INTRA-AAPL-x", "regime": "BULL",
             "regime_ts": "2026-08-31T13:12:00-07:00",
             "regime_detail": {"vol_composite": "BULL", "any_stale": False}},
            {"ts": "2026-09-01T08:00:00-07:00", "event": "exit", "symbol": "AAPL",
             "price": 110.0, "size": 1, "pnl": 10.0, "reason": "take_profit",
             "direction": "long", "trade_id": "INTRA-AAPL-x"},
        ]
        recs, _ = R.reduce_intraday(evs)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["regime"], "BULL")
        self.assertEqual(recs[0]["indicators"]["regime_ts"], "2026-08-31T13:12:00-07:00")
        self.assertEqual(recs[0]["indicators"]["regime_detail"]["vol_composite"], "BULL")

    def test_legacy_row_without_regime_is_unknown(self):
        evs = [
            {"ts": "2026-09-01T07:00:00-07:00", "event": "entry", "symbol": "AAPL",
             "price": 100.0, "size": 1, "score": 9, "direction": "long", "stop": 95.0, "target": 110.0},
            {"ts": "2026-09-01T08:00:00-07:00", "event": "exit", "symbol": "AAPL",
             "price": 110.0, "size": 1, "pnl": 10.0, "reason": "take_profit", "direction": "long"},
        ]
        recs, _ = R.reduce_intraday(evs)
        self.assertEqual(recs[0]["regime"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
