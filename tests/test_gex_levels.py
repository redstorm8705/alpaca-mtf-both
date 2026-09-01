#!/usr/bin/env python3
# ruff: noqa: E501
"""
Unit tests for data/gex.get_gex_levels — the READ-ONLY per-symbol pin/wall levels accessor
(day-tier Layer B prerequisite). Mocks the snapshot file via gex._SNAP_PATH and uses REAL
timestamps (fresh vs 90-min-old) so the shared stale clock is exercised, not bypassed.

Proves:
  * fresh entry with a resolved pin  -> levels_ok True, correct centroid/wall/call_wall/put_wall
  * 90-min-old entry                 -> STALE, levels_ok False, None levels (shares the regime clock)
  * missing symbol / error entry     -> UNKNOWN, levels_ok False
  * no snapshot file                 -> UNKNOWN
  * fresh entry, pin kind='none'     -> label kept, levels_ok False, spot/dte surfaced, None levels
  * leveraged tracker (TSLL)         -> resolves to the underlying (TSLA) entry
  * malformed snapshot JSON          -> UNKNOWN (never raises)

Runs with plain unittest:  python3 -m unittest tests.test_gex_levels
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

# gex.py imports `requests` at load; stub it so the harness runs without the dependency.
sys.modules.setdefault("requests", mock.MagicMock())

from data import gex  # noqa: E402

PT = ZoneInfo("America/Los_Angeles")
_FMT = "%Y-%m-%d %I:%M %p PT"


def _ts(minutes_ago: float) -> str:
    return (datetime.now(PT) - timedelta(minutes=minutes_ago)).strftime(_FMT)


def _pin(centroid=101.0, wall=100.0, call_wall=105.0, put_wall=95.0, kind="centroid+wall"):
    return {"kind": kind, "centroid": centroid, "wall": wall, "call_wall": call_wall,
            "put_wall": put_wall, "call_wall_frac": 0.4, "put_wall_frac": 0.5,
            "dispersion": 2.5, "confidence": 0.7, "atm_capture": 0.6, "expiry": "2026-09-05",
            "note": "test pin"}


def _entry(*, minutes_ago=1.0, label="POSITIVE", pin=None, spot=100.5, dte=4):
    e = {"label": label, "raw_gex_m": 3.2, "flip_strike": 99.0, "spot": spot, "dte": dte,
         "confirmed_ts": _ts(minutes_ago), "window": "weekly"}
    if pin is not None:
        e["pin"] = pin
    return e


def _snap(symbols: dict, ts_min_ago=1.0):
    return {"ts": _ts(ts_min_ago), "symbols": symbols}


class GexLevels(unittest.TestCase):
    def _patch_snap(self, snap_obj_or_text):
        m = mock.MagicMock()
        m.exists.return_value = True
        m.read_text.return_value = (snap_obj_or_text if isinstance(snap_obj_or_text, str)
                                    else json.dumps(snap_obj_or_text))
        return mock.patch.object(gex, "_SNAP_PATH", m)

    # 1 -- fresh entry with a resolved pin -> levels_ok True + correct levels
    def test_fresh_pin_levels(self):
        with self._patch_snap(_snap({"NVDA": _entry(pin=_pin())})):
            r = gex.get_gex_levels("NVDA")
        self.assertTrue(r["levels_ok"])
        self.assertEqual(r["label"], "POSITIVE")
        self.assertEqual(r["centroid"], 101.0)
        self.assertEqual(r["wall"], 100.0)
        self.assertEqual(r["call_wall"], 105.0)
        self.assertEqual(r["put_wall"], 95.0)
        self.assertEqual(r["confidence"], 0.7)
        self.assertEqual(r["dte"], 4)

    # 2 -- 90-min-old entry -> STALE, no levels (shares the get_gex_regime stale clock)
    def test_stale_entry_no_levels(self):
        with self._patch_snap(_snap({"NVDA": _entry(minutes_ago=90, pin=_pin())}, ts_min_ago=90)):
            r = gex.get_gex_levels("NVDA")
        self.assertEqual(r["label"], "STALE")
        self.assertFalse(r["levels_ok"])
        self.assertIsNone(r["centroid"])
        self.assertIsNone(r["call_wall"])

    # 3 -- symbol not in snapshot -> UNKNOWN
    def test_missing_symbol_unknown(self):
        with self._patch_snap(_snap({"AAPL": _entry(pin=_pin())})):
            r = gex.get_gex_levels("NVDA")
        self.assertEqual(r["label"], "UNKNOWN")
        self.assertFalse(r["levels_ok"])

    # 4 -- error entry -> UNKNOWN
    def test_error_entry_unknown(self):
        with self._patch_snap(_snap({"NVDA": {"error": "no_contracts"}})):
            r = gex.get_gex_levels("NVDA")
        self.assertEqual(r["label"], "UNKNOWN")
        self.assertFalse(r["levels_ok"])

    # 5 -- no snapshot file -> UNKNOWN
    def test_no_snapshot_unknown(self):
        m = mock.MagicMock()
        m.exists.return_value = False
        with mock.patch.object(gex, "_SNAP_PATH", m):
            r = gex.get_gex_levels("NVDA")
        self.assertEqual(r["label"], "UNKNOWN")
        self.assertFalse(r["levels_ok"])

    # 6 -- fresh entry but the pin never resolved (kind='none') -> label kept, no levels
    def test_unresolved_pin_no_levels(self):
        p = {"kind": "none", "centroid": None, "wall": None, "call_wall": None, "put_wall": None,
             "dispersion": None, "confidence": 0.0, "atm_capture": 0.0, "expiry": None, "note": "no data"}
        with self._patch_snap(_snap({"NVDA": _entry(label="NEAR-FLIP", pin=p, spot=100.5, dte=4)})):
            r = gex.get_gex_levels("NVDA")
        self.assertEqual(r["label"], "NEAR-FLIP")
        self.assertFalse(r["levels_ok"])
        self.assertIsNone(r["centroid"])
        self.assertEqual(r["spot"], 100.5)     # spot/dte still surfaced
        self.assertEqual(r["dte"], 4)

    # 7 -- leveraged tracker resolves to the underlying's entry
    def test_tracker_resolves_to_underlying(self):
        with self._patch_snap(_snap({"TSLA": _entry(label="NEGATIVE", pin=_pin(centroid=250.0))})):
            r = gex.get_gex_levels("TSLL")     # 1.5x TSLA
        self.assertTrue(r["levels_ok"])
        self.assertEqual(r["centroid"], 250.0)
        self.assertEqual(r["label"], "NEGATIVE")

    # 8 -- malformed JSON -> UNKNOWN, never raises
    def test_malformed_json_unknown(self):
        with self._patch_snap("{not valid json"):
            r = gex.get_gex_levels("NVDA")
        self.assertEqual(r["label"], "UNKNOWN")
        self.assertFalse(r["levels_ok"])

    # 9b -- fresh entry with label='UNKNOWN' (quality gate quarantined the LABEL) but a RESOLVED
    # pin -> levels_ok True + real levels. Consumers must gate on levels_ok, NOT label != 'UNKNOWN'.
    def test_content_unknown_with_resolved_pin(self):
        with self._patch_snap(_snap({"NVDA": _entry(label="UNKNOWN", pin=_pin(centroid=101.0))})):
            r = gex.get_gex_levels("NVDA")
        self.assertEqual(r["label"], "UNKNOWN")
        self.assertTrue(r["levels_ok"], "a quarantined LABEL must not drop a resolved pin")
        self.assertEqual(r["centroid"], 101.0)

    # 9c -- NEGATIVE label aged into the asymmetric NEG window (stale for NEG, fresh for base)
    # -> STALE (independently exercises the min(stale, stale_neg) branch)
    def test_negative_asymmetric_stale(self):
        import config as _cfg
        _base = getattr(_cfg, "GEX_STALE_MINUTES", 30)
        _neg = getattr(_cfg, "GEX_STALE_MINUTES_NEG", _base)
        if _neg >= _base:
            self.skipTest("no asymmetric NEG window configured")
        _aged = (_neg + _base) / 2.0   # between the NEG and base thresholds
        with self._patch_snap(_snap({"NVDA": _entry(minutes_ago=_aged, label="NEGATIVE", pin=_pin())}, ts_min_ago=_aged)):
            r = gex.get_gex_levels("NVDA")
        self.assertEqual(r["label"], "STALE")
        self.assertFalse(r["levels_ok"])

    # 9 -- result shape is stable across every path
    def test_shape_stable(self):
        with self._patch_snap(_snap({"NVDA": _entry(pin=_pin())})):
            r = gex.get_gex_levels("NVDA")
        for k in ("label", "spot", "centroid", "wall", "call_wall", "put_wall",
                  "confidence", "dispersion", "expiry", "dte", "age_minutes", "levels_ok"):
            self.assertIn(k, r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
