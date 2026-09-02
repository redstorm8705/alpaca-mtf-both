#!/usr/bin/env python3
# ruff: noqa: E501
"""
Unit tests for run_day_tier_shadow (Day-Tier SHADOW LOGGER, READ-ONLY + INERT).
Mocks the three pipeline entry points + the universe/equity helpers + a temp log path, and proves
the ENTER/WAIT/error paths, that it PLACES NO order, logs every symbol, and never raises.

Runs with plain unittest:  python3 -m unittest tests.test_day_tier_shadow
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock
import unittest

# The pipeline modules pull in data.fetcher (alpaca) / requests transitively; stub the SDK so the
# harness runs without the dependency (the pipeline functions themselves are mock-patched below).
for _mod in ("alpaca", "alpaca.data", "alpaca.data.timeframe", "alpaca.data.requests",
             "alpaca.data.historical", "alpaca.trading", "alpaca.trading.client",
             "alpaca.trading.requests", "alpaca.trading.enums", "requests"):
    sys.modules.setdefault(_mod, mock.MagicMock())

import run_day_tier_shadow as sh  # noqa: E402


def _decision(would_consider=True, conviction=0.5, action="FADE"):
    return {"symbol": "NVDA", "side": "LONG", "gex_action": action, "conviction": conviction,
            "would_consider": would_consider}


def _trigger(trigger="ENTER", entry_ref=100.0, direction="short"):
    return {"symbol": "NVDA", "trigger": trigger, "direction": direction, "mode": "FADE",
            "entry_ref": entry_ref, "target": 101.0, "wall_ref": 100.0, "vol_confirmed": True}


def _size(shares=1):
    return {"symbol": "NVDA", "shares": shares, "notional": shares * 100.0, "size_ok": shares >= 1}


class DayTierShadow(unittest.TestCase):
    def _patch(self, decision_fn, trigger_fn, size_fn, universe=("NVDA",), equity=2500.0, log=None):
        return (
            mock.patch("strategy.day_tier_decision.compute_day_tier_decision", decision_fn),
            mock.patch("strategy.day_tier_entry_trigger.compute_entry_trigger", trigger_fn),
            mock.patch("strategy.day_tier_sizing.compute_day_tier_size", size_fn),
            mock.patch.object(sh, "_universe", mock.Mock(return_value=list(universe))),
            mock.patch.object(sh, "_equity", mock.Mock(return_value=equity)),
            mock.patch.object(sh, "_SHADOW_LOG", log),
        )

    def _run(self, **kw):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "day_tier_shadow.jsonl"
            patches = self._patch(log=log, **kw)
            for p in patches:
                p.start()
            try:
                summary = sh.run_day_tier_shadow()
                lines = log.read_text().splitlines() if log.exists() else []
                records = [json.loads(x) for x in lines]
            finally:
                for p in patches:
                    p.stop()
        return summary, records

    # 1 -- full ENTER path: decision + trigger + size all logged; summary counts
    def test_enter_path_logs_full_stack(self):
        s, recs = self._run(
            decision_fn=mock.Mock(return_value=_decision()),
            trigger_fn=mock.Mock(return_value=_trigger("ENTER")),
            size_fn=mock.Mock(return_value=_size(1)),
        )
        self.assertEqual(s["enter_signals"], 1)
        self.assertEqual(s["would_consider"], 1)
        self.assertEqual(len(recs), 1)
        self.assertIsNotNone(recs[0]["decision"])
        self.assertIsNotNone(recs[0]["trigger"])
        self.assertIsNotNone(recs[0]["size"])           # size computed on ENTER
        self.assertTrue(s["logged"])

    # 2 -- WAIT path: no size computed; still logged
    def test_wait_path_no_size(self):
        size_fn = mock.Mock(return_value=_size(1))
        s, recs = self._run(
            decision_fn=mock.Mock(return_value=_decision(would_consider=True)),
            trigger_fn=mock.Mock(return_value=_trigger("WAIT", entry_ref=None)),
            size_fn=size_fn,
        )
        self.assertEqual(s["enter_signals"], 0)
        self.assertIsNone(recs[0]["size"])
        size_fn.assert_not_called()                      # sizing skipped on WAIT

    # 3 -- a per-symbol failure is recorded, does not abort the sweep, never raises
    def test_per_symbol_failure_isolated(self):
        s, recs = self._run(
            decision_fn=mock.Mock(side_effect=RuntimeError("boom")),
            trigger_fn=mock.Mock(return_value=_trigger()),
            size_fn=mock.Mock(return_value=_size()),
            universe=("NVDA", "AAPL"),
        )
        self.assertEqual(s["errors"], 2)                 # both symbols errored on decision
        self.assertEqual(len(recs), 2)                   # both still logged with error set
        self.assertIsNotNone(recs[0]["error"])

    # 4 -- equity None -> size skipped even on ENTER (no equity to size against)
    def test_no_equity_skips_size(self):
        size_fn = mock.Mock(return_value=_size(1))
        s, recs = self._run(
            decision_fn=mock.Mock(return_value=_decision()),
            trigger_fn=mock.Mock(return_value=_trigger("ENTER")),
            size_fn=size_fn,
            equity=None,
        )
        self.assertIsNone(recs[0]["size"])
        size_fn.assert_not_called()

    # 5 -- multi-symbol: one ENTER, one WAIT -> counts correct, both logged
    def test_multi_symbol_counts(self):
        def dec(sym):
            return _decision(would_consider=True)

        def trig(sym, decision):
            return _trigger("ENTER" if sym == "NVDA" else "WAIT",
                            entry_ref=100.0 if sym == "NVDA" else None)
        s, recs = self._run(
            decision_fn=mock.Mock(side_effect=dec),
            trigger_fn=mock.Mock(side_effect=trig),
            size_fn=mock.Mock(return_value=_size(1)),
            universe=("NVDA", "AAPL"),
        )
        self.assertEqual(s["symbols"], 2)
        self.assertEqual(s["enter_signals"], 1)
        self.assertEqual(len(recs), 2)

    # 6 -- shadow_scan_symbol never raises on a garbage pipeline return
    def test_scan_never_raises(self):
        with mock.patch("strategy.day_tier_decision.compute_day_tier_decision", mock.Mock(side_effect=ValueError("x"))):
            r = sh.shadow_scan_symbol("NVDA", 2500.0)
        self.assertEqual(r["symbol"], "NVDA")
        self.assertIsNotNone(r["error"])
        self.assertIsNone(r["size"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
