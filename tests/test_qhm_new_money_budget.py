# ruff: noqa: E501
"""QHM new-money budget (Rafael approved 2026-09-27): grandfathered holds (entered before the
2026-09-06 cap) no longer consume the 40% aggregate budget; the 20% per-name ceiling still applies.
Reproduces the live 2026-09-28 failure: NVDA/GOOGL/GE blocked every cycle with "room 0" because
LLY (~93% of equity) and GEV counted against the 40% aggregate."""
import unittest

from execution import quarterly_hold_manager as qm


def _mgr(positions: dict, prices: dict, gf=None) -> qm.QuarterlyHoldManager:
    m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
    m.dry_run = False
    m._positions = positions
    m._get_live_price = lambda sym: prices.get(sym)  # type: ignore[method-assign]
    # mirror manager init: pre-cap holds record their share count once
    m._gf_shares = dict(gf) if gf is not None else {
        s: p.qty_filled for s, p in positions.items() if qm.QuarterlyHoldManager._is_grandfathered(p)}
    return m


def _pos(sym: str, qty: int, entry_day, state=qm.HoldState.ACTIVE) -> qm.HoldPosition:
    return qm.HoldPosition(symbol=sym, direction="long", target_equity_pct=0.2, state=state,
                           qty_filled=qty, entry_day=entry_day)


LIVE_BOOK = {  # the 2026-09-28 production book
    "LLY": _pos("LLY", 2, "2026-08-24"),
    "GEV": _pos("GEV", 1, "2026-08-19"),
    "NVDA": _pos("NVDA", 0, None, qm.HoldState.PENDING_ENTRY),
    "GOOGL": _pos("GOOGL", 0, None, qm.HoldState.PENDING_ENTRY),
    "GE": _pos("GE", 0, None, qm.HoldState.PENDING_ENTRY),
}
PRICES = {"LLY": 1183.5, "GEV": 953.0, "NVDA": 231.0, "GOOGL": 341.0, "GE": 318.0}
EQUITY = 2532.18


class NewMoneyBudget(unittest.TestCase):
    def test_live_failure_reproduced_and_fixed(self):
        m = _mgr(dict(LIVE_BOOK), PRICES)
        # before the fix the aggregate counted LLY+GEV ($3,320 > 40% of $2,532) -> 0 room
        self.assertEqual(_mgr(dict(LIVE_BOOK), PRICES, gf={})._qhm_cap_room_shares("NVDA", 231.0, EQUITY), 0)
        # after: grandfathered holds excluded -> NVDA/GOOGL/GE each have room (per-name 20% binds)
        self.assertEqual(m._qhm_cap_room_shares("NVDA", 231.0, EQUITY), int(0.2 * EQUITY / 231.0))
        self.assertEqual(m._qhm_cap_room_shares("GOOGL", 341.0, EQUITY), int(0.2 * EQUITY / 341.0))
        self.assertEqual(m._qhm_cap_room_shares("GE", 318.0, EQUITY), int(0.2 * EQUITY / 318.0))

    def test_per_name_cap_still_blocks_grandfathered(self):
        m = _mgr(dict(LIVE_BOOK), PRICES)
        self.assertEqual(m._qhm_cap_room_shares("LLY", 1183.5, EQUITY), 0)   # 93% > 20%
        self.assertEqual(m._qhm_cap_room_shares("GEV", 953.0, EQUITY), 0)    # 38% > 20%

    def test_new_money_aggregate_still_binds(self):
        book = dict(LIVE_BOOK)
        book["NVDA"] = _pos("NVDA", 2, "2026-09-29")   # $462 new money
        book["GOOGL"] = _pos("GOOGL", 1, "2026-09-29")  # $341 new money -> $803 of $1,013 budget
        m = _mgr(book, PRICES)
        # aggregate room $210 -> 0 GE shares at $318 even though GE's per-name room is ~$506
        self.assertEqual(m._qhm_cap_room_shares("GE", 318.0, EQUITY), 0)

    def test_grandfather_rule_boundaries(self):
        g = qm.QuarterlyHoldManager._is_grandfathered
        self.assertTrue(g(_pos("X", 1, "2026-09-05")))
        self.assertFalse(g(_pos("X", 1, "2026-09-06")))      # entered on the cap date = new money
        self.assertFalse(g(_pos("X", 0, "2026-08-01")))      # no shares -> not grandfathered
        self.assertFalse(g(_pos("X", 3, None)))              # no entry day -> not grandfathered

    def test_fail_closed_on_unreadable_counted_price_only(self):
        book = dict(LIVE_BOOK)
        book["NVDA"] = _pos("NVDA", 1, "2026-09-29")
        prices = dict(PRICES)
        prices["NVDA"] = None                                 # counted new-money hold unreadable
        self.assertIsNone(_mgr(book, prices)._qhm_cap_room_shares("GE", 318.0, EQUITY))
        prices = dict(PRICES)
        prices["LLY"] = None                                  # only a grandfathered price unreadable
        self.assertIsNotNone(_mgr(dict(LIVE_BOOK), prices)._qhm_cap_room_shares("GE", 318.0, EQUITY))

    def test_shares_added_to_grandfathered_name_count_as_new_money(self):
        # cold-2nd round-2 repro: LLY grandfathered at 10 sh, then +10 sh added; N1 then N2 must not
        # push NEW money past 40% (LLY's added 10% + N1 + N2 <= 40%) whatever the order of buys.
        eq, px = 10000.0, 100.0
        gf = {"LLY": 10}
        book = {"LLY": _pos("LLY", 10, "2026-08-24")}
        prices = {"LLY": px, "N1": px, "N2": px}
        self.assertEqual(_mgr(book, prices, gf)._qhm_cap_room_shares("LLY", px, eq), 10)   # per-name 20% binds
        book["LLY"] = _pos("LLY", 20, "2026-08-24")                                       # the add filled
        self.assertEqual(_mgr(book, prices, gf)._qhm_cap_room_shares("N1", px, eq), 20)    # per-name 20% binds
        book["N1"] = _pos("N1", 20, "2026-09-29")
        self.assertEqual(_mgr(book, prices, gf)._qhm_cap_room_shares("N2", px, eq), 10)    # 40% - (10% + 20%)
        # a trim below the recorded baseline shrinks the exemption (never negative counted shares)
        book2 = {"LLY": _pos("LLY", 5, "2026-08-24")}
        self.assertEqual(_mgr(book2, prices, gf)._qhm_total_notional(exclude_grandfathered=True), 0.0)
        # a re-entered name (new entry_day) gets no exemption even if a stale baseline exists
        book3 = {"LLY": _pos("LLY", 10, "2026-10-01")}
        self.assertEqual(_mgr(book3, prices, gf)._qhm_total_notional(exclude_grandfathered=True), 1000.0)

    def test_exemption_never_grows_back_after_a_reduction(self):
        # risk seat r3 repro: baseline 2 -> earnings trim to 1 -> dip-add back to 2: the re-bought share is NEW money
        prices = {"LLY": 1183.5}
        m = _mgr({"LLY": _pos("LLY", 1, "2026-08-24")}, prices, gf={"LLY": 2})
        with self.subTest("trim ratchets"):
            self.assertEqual(m._qhm_total_notional(exclude_grandfathered=True), 0.0)
            self.assertEqual(m._gf_shares["LLY"], 1)
        m._positions["LLY"] = _pos("LLY", 2, "2026-08-24")
        self.assertAlmostEqual(m._qhm_total_notional(exclude_grandfathered=True), 1183.5)

    def test_closed_record_with_stale_qty_not_counted(self):
        book = dict(LIVE_BOOK)
        book["GE"] = _pos("GE", 1, "2026-09-29", state=qm.HoldState.CLOSED)
        prices = dict(PRICES)
        prices["GE"] = None          # a closed name's unreadable price must not fail-close the book
        self.assertEqual(_mgr(book, prices)._qhm_total_notional(exclude_grandfathered=True), 0.0)

    def test_manager_init_records_baseline_once(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            sp, gp = Path(d) / "q.json", Path(d) / "gf.json"
            sp.write_text(json.dumps({k: v.to_dict() for k, v in LIVE_BOOK.items()}))
            with mock.patch.object(qm, "_GRANDFATHER_STATE_PATH", gp):
                m = qm.QuarterlyHoldManager(broker=None, fmp_client=None, alerter=None, config={},
                                            state_path=sp, dry_run=False)
                self.assertEqual(m._gf_shares, {"LLY": 2, "GEV": 1})
                self.assertEqual(json.loads(gp.read_text()), {"LLY": 2, "GEV": 1})
                # a CLOSED pre-cap record is not recorded
                gp.unlink()
                saved = json.loads(sp.read_text())
                saved["GEV"]["state"] = "CLOSED"
                sp.write_text(json.dumps(saved))
                mc = qm.QuarterlyHoldManager(broker=None, fmp_client=None, alerter=None, config={},
                                             state_path=sp, dry_run=False)
                self.assertEqual(mc._gf_shares, {"LLY": 2})
                # a second init never re-records (a later add must not grow the exemption)
                saved = json.loads(sp.read_text())
                saved["LLY"]["qty_filled"] = 5
                sp.write_text(json.dumps(saved))
                m2 = qm.QuarterlyHoldManager(broker=None, fmp_client=None, alerter=None, config={},
                                             state_path=sp, dry_run=False)
                self.assertEqual(m2._gf_shares["LLY"], 2)
                # corrupt baseline file -> no exemptions, file NOT rewritten
                gp.write_text("{not json")
                m3 = qm.QuarterlyHoldManager(broker=None, fmp_client=None, alerter=None, config={},
                                             state_path=sp, dry_run=False)
                self.assertEqual(m3._gf_shares, {})
                self.assertEqual(gp.read_text(), "{not json")
                # quarterly_holds.json schema is unchanged (rollback-safe)
                self.assertNotIn("grandfathered_qty", json.loads(sp.read_text())["LLY"])

    def test_unparseable_entry_day_is_new_money(self):
        self.assertFalse(qm.QuarterlyHoldManager._is_grandfathered(_pos("X", 1, "08/24/2026")))
        self.assertTrue(qm.QuarterlyHoldManager._is_grandfathered(_pos("X", 1, "2026-08-24T09:00:00")))

    def test_bad_inputs_fail_closed(self):
        m = _mgr(dict(LIVE_BOOK), PRICES)
        self.assertIsNone(m._qhm_cap_room_shares("GE", 318.0, 0))
        self.assertIsNone(m._qhm_cap_room_shares("GE", 0, EQUITY))


if __name__ == "__main__":
    unittest.main()
