# ruff: noqa: E501
"""QHM-COVER-1109 — every QHM share keeps a stop through a dip-add (design record
logs/design_records/qhm_cover_2026-11-09.md, section 4 tests). Every broker, allocator, ledger and alert transport is
mocked; nothing reaches Alpaca, Slack or the ownership ledger file."""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401  — test guard
import alerts
from execution import ownership_guard as real_guard
from execution import quarterly_hold_manager as qm

TODAY = "2026-11-09"
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=qm.ET)


# ── _stop_safe_add fixtures (fix A, A2/X3, tests 1-3, 11, 12) ──────────────────────────────────────────────────────
class Dispatcher:
    def __init__(self, events, add_raises=False):
        self.events, self.add_raises = events, add_raises
        self.stops, self.adds = [], []

    def submit_gtc_stop(self, broker, sym, qty, side, price, client_order_id=None):
        self.events.append(("stop", qty))
        self.stops.append((sym, qty, side, price))
        return SimpleNamespace(id="stop-new")

    def submit_limit(self, broker, sym, qty, side, price, client_order_id=None):
        self.adds.append((sym, qty, side, price, client_order_id))
        self.events.append(("buy", client_order_id))
        if self.add_raises:
            raise ConnectionError("transport dropped after send")
        return SimpleNamespace(id="add-1", client_order_id=client_order_id, symbol=sym, side=side,
                               qty=qty, filled_qty=0, status="new")


def _add_mgr(pos, held):
    m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
    m.dry_run = False
    m.broker = SimpleNamespace(get_position=lambda s: SimpleNamespace(qty=held["qty"]))
    m._positions = {pos.symbol: pos}
    m.alerts = []
    m._alert = m.alerts.append
    m._save_state = lambda: True
    m._grandfathered_notional_for_allocator = lambda: 0.0
    m._now_et = lambda: NOW

    def _arm(p, client_id):
        p.ambiguous_add_client_order_id = client_id
        p.ambiguous_add_baseline_qty = p.qty_filled
        return True
    m._arm_ambiguous_add_recovery = _arm
    m._resync_from_alpaca = lambda p: None
    # Proves the caller's expected QHM quantity (the exact add-order proof), like the production helper does.
    m._strict_resync_qty = lambda p, hint=None, _base=None: hint if hint is not None else p.qty_filled
    return m


def _add_pos(qty=2):
    return qm.HoldPosition(symbol="NVDA", direction="long", target_equity_pct=0.2, state=qm.HoldState.ACTIVE,
                           qty_filled=qty, entry_day="2026-09-29", stop_price=163.07, stop_order_id="stop-old")


def _run_add(m, pos, *, statuses, events, lease=None, label="dip-add", dispatcher=None, recover=None):
    """statuses: the add order's (status, filled) on each get_order poll; the last one repeats."""
    held = {"qty": pos.qty_filled}
    m.broker = SimpleNamespace(get_position=lambda s: SimpleNamespace(qty=held["qty"]))
    d = dispatcher or Dispatcher(events)
    m._dispatcher = d
    polls = list(statuses)
    cancels = []

    def _get_order(oid):
        status, filled = polls.pop(0) if len(polls) > 1 else polls[0]
        if oid == "add-1" and "add-1" in cancels and status not in ("filled", "canceled"):
            status = "canceled"
        events.append(("poll", status))
        held["qty"] = pos.qty_filled + filled if status in ("filled", "partially_filled", "canceled") else held["qty"]
        coid = d.adds[-1][4] if d.adds else None
        return SimpleNamespace(id="add-1", client_order_id=coid, symbol="NVDA", side="buy", qty=1,
                               filled_qty=filled, status=status)

    def _cancel(oid):
        cancels.append(oid)
        return True
    broker_mod = SimpleNamespace(is_market_open=lambda: True, cancel_order=_cancel, get_order=_get_order,
                                 get_order_by_client_order_id=recover or (lambda _c: None))
    allocator_mod = SimpleNamespace(
        live_admit=lambda *a, **k: SimpleNamespace(approved=True, lease=lease),
        live_bind=lambda *a, **k: True, live_release=lambda *a, **k: True,
        live_order_id=lambda le: getattr(le, "client_order_id", None) if le is not None else None)
    credits = []
    guard_mod = SimpleNamespace(tier_of_coid=real_guard.tier_of_coid, make_coid=mock.Mock(wraps=real_guard.make_coid),
                                apply_authorized_tier_fill_increase=lambda *a: credits.append(a) or True)
    clock = iter(range(0, 1000))
    with mock.patch.dict(sys.modules, {"execution.broker": broker_mod,
                                       "execution.tier_capital_allocator": allocator_mod,
                                       "execution.ownership_guard": guard_mod}), \
            mock.patch("time.sleep"), mock.patch("time.monotonic", side_effect=lambda: float(next(clock))):
        out = m._stop_safe_add(pos, 1, 180.0, label)
    return out, d, credits, cancels, guard_mod


class AddCarriesQhmId(unittest.TestCase):
    def test_1_no_lease_add_gets_qhm_id_and_stop_waits_for_terminal_buy(self):
        for label in ("dip-add", "tranche-2"):          # test 11: the tranche caller's helper path too
            with self.subTest(label=label):
                pos, events = _add_pos(qty=2), []
                m = _add_mgr(pos, {"qty": 2})
                out, d, credits, _cancels, guard = _run_add(
                    m, pos, events=events, label=label,
                    statuses=[("new", 0), ("new", 0), ("filled", 1)])
                coid = d.adds[0][4]
                self.assertTrue(coid.startswith("QH-NVDA-b-"), coid)
                self.assertEqual(real_guard.tier_of_coid(coid), "qhm")
                guard.make_coid.assert_called_once()
                self.assertEqual(out, qm.AddOutcome(True, 1))
                # No stop goes in before the buy is proven terminal (Alpaca refuses a sell stop while a buy rests).
                first_stop = next(i for i, e in enumerate(events) if e[0] == "stop")
                self.assertEqual(events[first_stop - 1], ("poll", "filled"))
                self.assertNotIn("stop", [e[0] for e in events[:first_stop]])
                self.assertEqual(d.stops, [("NVDA", 3, "sell", 163.07)])
                self.assertEqual(credits, [("NVDA", "qhm", 1, 3, "add-1")])
                self.assertEqual(pos.last_dip_add_date, TODAY)
                self.assertEqual(pos.state, qm.HoldState.ACTIVE)
                self.assertIsNone(pos.ambiguous_add_client_order_id)

    def test_2_no_lease_buy_never_fills_stop_back_on_original_qty_date_written(self):
        for label in ("dip-add", "tranche-2"):
            with self.subTest(label=label):
                pos, events = _add_pos(qty=2), []
                m = _add_mgr(pos, {"qty": 2})
                out, d, credits, cancels, _ = _run_add(m, pos, events=events, label=label, statuses=[("new", 0)])
                self.assertEqual(out, qm.AddOutcome(True, 0))
                self.assertEqual(cancels, ["stop-old", "add-1"])
                self.assertEqual(d.stops, [("NVDA", 2, "sell", 163.07)])
                self.assertEqual(credits, [])
                self.assertEqual(pos.last_dip_add_date, TODAY)
                first_stop = next(i for i, e in enumerate(events) if e[0] == "stop")
                self.assertEqual(events[first_stop - 1], ("poll", "canceled"))

    def test_12_lease_present_uses_the_lease_id_unchanged(self):
        pos, events = _add_pos(qty=2), []
        m = _add_mgr(pos, {"qty": 2})
        lease = SimpleNamespace(id="lease-1", client_order_id="QH-NVDA-b-1-lease")
        out, d, credits, _c, guard = _run_add(m, pos, events=events, lease=lease, statuses=[("filled", 1)])
        self.assertEqual(d.adds[0][4], "QH-NVDA-b-1-lease")
        guard.make_coid.assert_not_called()
        self.assertEqual(out, qm.AddOutcome(True, 1))
        self.assertEqual(d.stops, [("NVDA", 3, "sell", 163.07)])

    def test_pre_submit_refusal_reports_not_submitted_and_writes_no_date(self):
        pos = _add_pos(qty=2)
        m = _add_mgr(pos, {"qty": 2})
        broker_mod = SimpleNamespace(is_market_open=lambda: False, cancel_order=None, get_order=None,
                                     get_order_by_client_order_id=None)
        with mock.patch.dict(sys.modules, {"execution.broker": broker_mod}):
            out = m._stop_safe_add(pos, 1, 180.0, "dip-add")
        self.assertEqual(out, qm.AddOutcome(False, 0))
        self.assertIsNone(pos.last_dip_add_date)


def _dip_mgr(pos, outcome):
    m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
    m.dry_run = False
    m._positions = {pos.symbol: pos}
    m._now_et = lambda: NOW
    m._save_state = lambda: True
    m._get_live_price = lambda s: 200.0
    m._get_account_equity = lambda: 2700.0
    m._qhm_cap_room_shares = lambda s, p, e: 5
    m.broker = SimpleNamespace(get_account=lambda: SimpleNamespace(regt_buying_power=10000.0))
    m._stop_safe_add = mock.Mock(return_value=outcome)
    return m


def _dip_pos():
    return qm.HoldPosition(symbol="NVDA", direction="long", target_equity_pct=0.2, state=qm.HoldState.ACTIVE,
                           qty_filled=1, avg_entry_price=226.36, tranche1_price=226.36, stop_price=192.41,
                           stop_order_id="stop-1")


class SubmittedAddCounts(unittest.TestCase):
    def _dip(self, m, pos):
        with mock.patch("execution.dip_zones.zones_for", return_value=(-0.039, -0.085)):
            m._maybe_dip_add(pos, TODAY)

    def test_counter_counts_fill_or_open_buy_never_a_refusal(self):
        cases = [(qm.AddOutcome(True, 1), 1, TODAY), (qm.AddOutcome(True, 0, open_buy=True), 1, TODAY),
                 (qm.AddOutcome(True, 0), 0, TODAY), (qm.AddOutcome(False, 0), 0, None)]
        for outcome, count, date in cases:
            with self.subTest(outcome=outcome):
                pos = _dip_pos()
                m = _dip_mgr(pos, outcome)
                self._dip(m, pos)
                m._stop_safe_add.assert_called_once()
                self.assertEqual((pos.dip_adds_quarter, pos.last_dip_add_date), (count, date))

    def test_3_second_dip_add_same_day_after_submitted_uncertain_add_is_blocked(self):
        # The real helper: the buy is sent, the transport drops and the id lookup cannot find it (uncertain).
        pos, events = _add_pos(qty=1), []
        pos.avg_entry_price = pos.tranche1_price = 226.36
        m = _add_mgr(pos, {"qty": 1})
        out, d, _cr, _c, _g = _run_add(m, pos, events=events, statuses=[("new", 0)],
                                       dispatcher=Dispatcher(events, add_raises=True))
        self.assertEqual(out, qm.AddOutcome(True, 0))
        self.assertEqual(len(d.adds), 1)
        self.assertEqual(pos.last_dip_add_date, TODAY)      # written before the send, so the error cannot erase it
        # The next cycle the same day: the dip-add path never reaches the helper again.
        pos.state = qm.HoldState.ACTIVE
        m2 = _dip_mgr(pos, qm.AddOutcome(True, 1))
        self._dip(m2, pos)
        m2._stop_safe_add.assert_not_called()

    def test_11_tranche_caller_submitted_no_fill_uses_the_day(self):
        from tests.test_qhm_tranche_adds import _mgr as tranche_mgr, _pos as tranche_pos
        pos = tranche_pos()
        m = tranche_mgr(pos, fill=0, submitted=True)
        m.maybe_enter_positions()
        m.maybe_enter_positions()
        self.assertEqual(len(m.calls), 1)                   # a sent buy blocks a same-day retry
        self.assertEqual(pos.tranches_filled, 1)            # no fill -> no tranche advance


# ── legacy cover recovery (fix B, tests 4-9, X1, X6) ────────────────────────────────────────────────────────────────
def _order(oid, coid, side, filled, status="filled", qty=None, type_="limit", stop=None, created=1):
    return SimpleNamespace(id=oid, client_order_id=coid, symbol="NVDA", side=side, qty=qty if qty is not None else filled,
                           filled_qty=filled, status=status, type=type_, stop_price=stop, time_in_force="gtc",
                           created_at=datetime(2026, 11, 9, 9, created, tzinfo=qm.ET), legs=None)


def _nvda_history():
    return [_order("b1", "QH-NVDA-b-1700000000000-aaaa0001", "buy", 1, created=1),
            _order("s1", "QH-NVDA-s-1700000000001-aaaa0002", "sell", 0, status="canceled", qty=1, type_="stop",
                   stop=163.07, created=2),
            _order("b2", "QH-NVDA-b-1700000000002-aaaa0003", "buy", 1, created=3),
            _order("b3", "QH-NVDA-b-1700000000003-aaaa0004", "buy", 1, created=4)]


def _resting(oid, coid, qty, stop, filled=0, type_="stop", status="new"):
    return _order(oid, coid, "sell", filled, status=status, qty=qty, type_=type_, stop=stop)


class FakeClient:
    def __init__(self, history, fail=False):
        self.history, self.fail, self.calls = history, fail, 0

    def get_orders(self, filter=None):
        """Alpaca-like: orders created after `after`, oldest first (ties by id), at most `limit` rows."""
        self.calls += 1
        if self.fail:
            raise ConnectionError("orders API down")
        rows = sorted((o for o in self.history if filter is None or filter.after is None
                       or o.created_at > filter.after), key=lambda o: (o.created_at, o.id))
        limit = getattr(filter, "limit", None)
        return rows[:limit] if limit else rows


def _nvda_state(**over):
    fields = dict(symbol="NVDA", direction="long", target_equity_pct=0.2, state=qm.HoldState.PENDING_STOP_REPLACE,
                  qty_filled=2, qty_total=2, stop_price=163.07, stop_order_id=None,
                  stop_recovery_requires_qty_proof=True, stop_recovery_canceled_order_id="s1",
                  stop_recovery_baseline_qty=1, stop_recovery_add_submitted=True,
                  ambiguous_add_client_order_id=None, created_at="2026-11-01T09:00:00-05:00")
    fields.update(over)
    return qm.HoldPosition(**fields)


class Scenario:
    """A QHM manager over a mocked broker module, trading client and in-memory ledger."""

    def __init__(self, pos, *, broker_qty=3, ledger=None, history=None, book=None, fills_fail=False,
                 position_fail=False, canceled_stop=None, by_coid=None):
        self.pos, self.book = pos, list(book or [])
        self.ledger = {"positions": {"NVDA": {"tiers": dict(ledger or {"qhm": {"qty": 2.0}, "intraday": {"qty": 1.0}})}}}
        self.broker_qty, self.position_fail = broker_qty, position_fail
        self.client = FakeClient(history if history is not None else _nvda_history(), fail=fills_fail)
        self.stops, self.alerts, self.ledger_writes = [], [], []
        self.canceled_stop = canceled_stop or SimpleNamespace(id="s1", status="canceled", filled_qty=0)
        self.by_coid = by_coid or {}
        m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
        m.dry_run = False
        m._positions = {pos.symbol: pos}
        m.alerts = self.alerts
        m._alert = self.alerts.append
        m._save_state = lambda: True
        m._now_et = lambda: NOW
        m._get_open_orders = lambda: list(self.book)
        m.broker = SimpleNamespace(get_position=self._position)
        m._dispatcher = SimpleNamespace(submit_gtc_stop=self._submit_stop)
        self.m = m

    def _position(self, sym):
        if self.position_fail:
            raise ConnectionError("positions API down")
        return SimpleNamespace(qty=self.broker_qty, side="long", current_price=180.0)

    def _submit_stop(self, broker, sym, qty, side, price, client_order_id=None):
        self.stops.append((sym, qty, side, price))
        placed = qty - getattr(self, "short_place", 0)        # a broker free-share retry may place fewer
        order = _resting("stop-cover", client_order_id, placed, price)
        self.book.append(order)
        return order

    def _replace(self, sym, oid, price, qty=None):
        old = next(o for o in self.book if o.id == oid)
        self.book.remove(old)
        order = _resting(oid + "-r", old.client_order_id, qty, price)
        self.book.append(order)
        self.stops.append(("replace", qty))
        return order

    def _get_order(self, oid):
        if oid == "s1":
            return self.canceled_stop
        return None

    def _reduce(self, sym, tier, fill, net, source):
        tiers = self.ledger["positions"][sym]["tiers"]
        total = sum(float(t["qty"]) for t in tiers.values())
        if tiers[tier].get("last_fill_id") == source and total == net:
            return True
        if total - fill != net:
            return False
        tiers[tier]["qty"] = float(tiers[tier]["qty"]) - fill
        tiers[tier]["last_fill_id"] = source
        self.ledger_writes.append(("reduce", fill, net, source))
        return True

    def _increase(self, sym, tier, fill, net, source):
        tiers = self.ledger["positions"][sym]["tiers"]
        total = sum(float(t["qty"]) for t in tiers.values())
        if total + fill != net:
            return False
        tiers[tier]["qty"] = float(tiers[tier]["qty"]) + fill
        tiers[tier]["last_fill_id"] = source
        self.ledger_writes.append(("increase", fill, net, source))
        return True

    def run(self):
        broker_mod = SimpleNamespace(get_order=self._get_order, get_trading_client=lambda: self.client,
                                     get_order_by_client_order_id=lambda c: self.by_coid.get(c),
                                     cancel_order=lambda oid: True, replace_stop_order=self._replace)
        with mock.patch.dict(sys.modules, {"execution.broker": broker_mod}), \
                mock.patch.object(real_guard, "load_ledger", lambda: self.ledger), \
                mock.patch.object(real_guard, "apply_authorized_tier_fill_reduction", self._reduce), \
                mock.patch.object(real_guard, "apply_authorized_tier_fill_increase", self._increase), \
                mock.patch("time.sleep"):
            with self_logs() as logs:
                result = self.m.resubmit_stop_if_needed("NVDA", list(self.book))
        self.logs = logs.output
        return result

    def qhm_ledger(self):
        return self.ledger["positions"]["NVDA"]["tiers"]["qhm"]["qty"]

    def later_cycle(self):
        """One ordinary ACTIVE cycle: the stop-coverage check, then the stray-sell guard, over one book snapshot."""
        self.cancelled = getattr(self, "cancelled", [])

        def _cancel(oid):
            self.cancelled.append(oid)
            self.book = [o for o in self.book if o.id != oid]
            return True
        broker_mod = SimpleNamespace(cancel_order=_cancel, get_open_orders=lambda: list(self.book))
        snapshot = list(self.book)
        with mock.patch.dict(sys.modules, {"execution.broker": broker_mod}), \
                mock.patch.object(real_guard, "load_ledger", lambda: self.ledger), \
                mock.patch("alerts.clear_gtc_failed"):
            self.m._reconcile_stop_coverage(self.pos, snapshot)
            self.m.cancel_stray_sell_orders(open_orders=snapshot, skip_symbols=set())


def self_logs():
    return unittest.TestCase().assertLogs("execution.quarterly_hold_manager", level="INFO")


def _action_pages(alerts_list):
    return [a for a in alerts_list if "ACTION NVDA" in a]


class LegacyCoverRecovery(unittest.TestCase):
    def test_4_todays_nvda_state_stop_two_page_the_third_ledger_untouched(self):
        s = Scenario(_nvda_state())
        self.assertTrue(s.run())
        self.assertEqual(s.stops, [("NVDA", 2, "sell", 163.07)])
        self.assertEqual(s.pos.state, qm.HoldState.ACTIVE)
        self.assertEqual((s.pos.qty_filled, s.pos.stop_order_id), (2, "stop-cover"))
        self.assertFalse(s.pos.stop_recovery_add_submitted or s.pos.stop_recovery_requires_qty_proof)
        self.assertIsNone(s.pos.stop_recovery_canceled_order_id)
        self.assertEqual(s.ledger_writes, [])                       # round 5: no ledger write in this recovery
        self.assertEqual(s.qhm_ledger(), 2.0)
        pages = _action_pages(s.alerts)
        self.assertEqual(len(pages), 1)
        self.assertIn("1 share(s) have no stop", pages[0])
        self.assertIn("$163.07", pages[0])
        self.assertTrue(any("cover recovery: proved=3 ledger_qhm=2 broker=3 protective_remaining=0 stop=2 gap=1 "
                            "price=163.07 action=sent" in line for line in s.logs), s.logs)

    def test_5_another_tier_holds_one_share_under_its_own_stop(self):
        s = Scenario(_nvda_state(), book=[_resting("dt1", "DT-NVDA-s-1-x", 1, 170.0)])
        self.assertTrue(s.run())
        self.assertEqual(s.stops, [("NVDA", 2, "sell", 163.07)])
        self.assertEqual(_action_pages(s.alerts), [])

    def test_6_owner_stop_for_three_rests_no_second_stop(self):
        s = Scenario(_nvda_state(), book=[_resting("own3", "manual-1", 3, 160.0)])
        self.assertFalse(s.run())
        self.assertEqual(s.stops, [])
        self.assertEqual(s.pos.state, qm.HoldState.PENDING_STOP_REPLACE)
        heads_up = [a for a in s.alerts if "covered by a stop the bot didn't place" in a]
        self.assertEqual(len(heads_up), 1)
        self.assertEqual(_action_pages(s.alerts), [])
        self.assertTrue(s.pos.stop_recovery_add_submitted)          # keeps re-checking each cycle
        s.run()
        self.assertEqual(len([a for a in s.alerts if "covered by a stop the bot didn't place" in a]), 1)
        # The owner cancels their stop: the next cycle covers QHM's shares.
        s.book = []
        self.assertTrue(s.run())
        self.assertEqual(s.stops, [("NVDA", 2, "sell", 163.07)])

    def test_owner_stop_for_one_rests_qhm_stops_two_no_gap(self):
        s = Scenario(_nvda_state(), book=[_resting("own1", "manual-1", 1, 160.0)])
        self.assertTrue(s.run())
        self.assertEqual(s.stops, [("NVDA", 2, "sell", 163.07)])
        self.assertEqual(_action_pages(s.alerts), [])

    def test_owner_stop_covers_part_of_qhm_then_cancelled_qhm_takes_over(self):
        s = Scenario(_nvda_state(), book=[_resting("own2", "manual-1", 2, 160.0)])
        self.assertFalse(s.run())
        self.assertEqual(s.stops, [("NVDA", 1, "sell", 163.07)])      # min(3, 2, 3 - 2) = 1
        self.assertEqual(s.pos.state, qm.HoldState.PENDING_STOP_REPLACE)
        self.assertTrue(s.pos.stop_recovery_add_submitted)
        s.book = [o for o in s.book if o.id != "own2"]                  # the owner cancels their stop
        self.assertTrue(s.run())
        self.assertEqual(s.stops[-1], ("replace", 2))                   # QHM's own stop resized in place
        self.assertEqual((s.pos.state, s.pos.stop_order_id), (qm.HoldState.ACTIVE, "stop-cover-r"))
        self.assertEqual(len(_action_pages(s.alerts)), 1)              # the third share has no stop

    def test_x1_limit_trailing_and_above_market_sells_are_not_coverage(self):
        # Each holds 1 share at Alpaca (so the QHM stop is sized to the free shares) but none is coverage.
        for order in (_resting("lim", "manual-1", 1, None, type_="limit"),
                      _resting("trail", "manual-2", 1, 150.0, type_="trailing_stop"),
                      _resting("high", "manual-3", 1, 190.0)):             # stop above the $180 price
            with self.subTest(order=order.id):
                s = Scenario(_nvda_state(), book=[order])
                self.assertTrue(s.run())
                self.assertEqual(s.stops, [("NVDA", 2, "sell", 163.07)])   # min(2, 3 - 1 open sell)
                pages = _action_pages(s.alerts)
                self.assertEqual(len(pages), 1)
                self.assertIn("1 share(s) have no stop", pages[0])         # 3 - 0 protective - 2

    def test_owner_stop_plus_owner_limit_sell_qhm_stops_the_free_share(self):
        # Cold review round 2: broker 3, owner stop 1 @ $163 + owner limit sell 1 above the price.
        limit = _resting("lim", "manual-2", 1, None, type_="limit")
        limit.limit_price = 190.0
        s = Scenario(_nvda_state(), book=[_resting("own1", "manual-1", 1, 163.0), limit])
        s.run()
        self.assertEqual(s.stops, [("NVDA", 1, "sell", 163.07)])           # min(2, 3 - 2 open sells)
        pages = _action_pages(s.alerts)
        self.assertEqual(len(pages), 1)
        self.assertIn("1 share(s) have no stop", pages[0])                 # 3 - 1 protective - 1
        self.assertEqual(s.pos.state, qm.HoldState.PENDING_STOP_REPLACE)   # keeps re-checking for the held share
        # No share is free, so the page names the owner's limit order instead of asking for a new stop.
        self.assertNotIn("place a sell stop", pages[0])
        self.assertIn("held by your own order(s) (limit sell for 1 share(s) @ $190.00)", pages[0])
        self.assertIn("cancel or change that order to a sell stop at $163.07", pages[0])

    def test_free_share_gap_still_asks_for_a_stop(self):
        s = Scenario(_nvda_state())
        s.run()
        page = _action_pages(s.alerts)[0]
        self.assertIn("place a sell stop for 1 share(s) at $163.07", page)
        self.assertNotIn("held by your own order", page)

    def test_pending_new_qhm_stop_is_not_an_unclear_stop_book(self):
        # Cycle 1 placed a partial cover stop (owner stop 2 rests); cycle 2 reads it back still pending_new.
        s = Scenario(_nvda_state(), book=[_resting("own2", "manual-1", 2, 160.0)])
        s.run()
        self.assertEqual(s.stops, [("NVDA", 1, "sell", 163.07)])
        qh = next(o for o in s.book if o.id == "stop-cover")
        qh.status = "pending_new"
        s.run()
        self.assertFalse(any("ambiguous QH stop book" in a or "not the exact cover" in a for a in s.alerts),
                         s.alerts)
        self.assertEqual(s.stops, [("NVDA", 1, "sell", 163.07)])          # adopted, not doubled

    def test_pending_new_stop_after_cover_recovery_confirms_on_the_next_cycle(self):
        s = Scenario(_nvda_state())
        self.assertTrue(s.run())
        next(o for o in s.book if o.id == "stop-cover").status = "pending_new"
        s.later_cycle()
        self.assertEqual(s.pos.state, qm.HoldState.ACTIVE)
        self.assertFalse(any("ambiguous QH stop candidate" in a for a in s.alerts), s.alerts)

    def test_other_holds_keep_the_original_stop_status_rule(self):
        pos = qm.HoldPosition(symbol="NVDA", direction="long", target_equity_pct=0.2, stop_price=163.07)
        order = _resting("x", "QH-NVDA-s-1-aaaa0001", 2, 163.07, status="pending_new")
        self.assertIsNone(qm.QuarterlyHoldManager._qh_stop_shape(order, pos))
        self.assertIsNotNone(qm.QuarterlyHoldManager._qh_stop_shape(
            order, pos, qm.QuarterlyHoldManager._PROTECTIVE_OPEN_STATUSES))

    def test_broker_places_fewer_shares_than_asked_records_the_actual_qty(self):
        s = Scenario(_nvda_state())
        s.short_place = 1
        s.run()
        self.assertEqual(s.stops, [("NVDA", 2, "sell", 163.07)])
        self.assertEqual(s.pos.stop_order_id, "stop-cover")
        self.assertNotEqual(s.pos.state, qm.HoldState.ACTIVE)              # 1 placed < 2 owned
        pages = _action_pages(s.alerts)
        self.assertEqual(len(pages), 1)
        self.assertIn("2 share(s) have no stop", pages[0])                 # 3 - 0 - 1 placed

    def test_x6_partly_filled_owner_stop_counts_its_remaining_quantity(self):
        history = [_order(f"b{i}", f"QH-NVDA-b-17000000000{i}-aaaa000{i}", "buy", 1, created=i) for i in range(1, 4)]
        s = Scenario(_nvda_state(), broker_qty=5, ledger={"qhm": {"qty": 3.0}, "intraday": {"qty": 2.0}},
                     history=history, book=[_resting("own", "manual-1", 3, 160.0, filled=1, status="partially_filled")])
        self.assertTrue(s.run())
        self.assertEqual(s.stops, [("NVDA", 3, "sell", 163.07)])     # 5 - 2 remaining = 3; min(3, 3, 3)
        self.assertEqual(_action_pages(s.alerts), [])

    def test_7_fills_or_position_unreadable_nothing_placed_and_paged(self):
        for kw in ({"fills_fail": True}, {"position_fail": True}):
            with self.subTest(**kw):
                s = Scenario(_nvda_state(), **kw)
                self.assertFalse(s.run())
                self.assertEqual(s.stops, [])
                self.assertEqual(s.pos.state, qm.HoldState.PENDING_STOP_REPLACE)
                self.assertTrue(any("cover recovery input unreadable" in a for a in s.alerts), s.alerts)

    def test_malformed_protective_quantity_stays_pending(self):
        s = Scenario(_nvda_state(), book=[_resting("bad", "manual-1", 2.5, 160.0)])
        self.assertFalse(s.run())
        self.assertEqual(s.stops, [])
        self.assertEqual(s.pos.state, qm.HoldState.PENDING_STOP_REPLACE)

    def test_8_fills_prove_more_than_the_broker_holds_capped_at_broker(self):
        history = [_order(f"b{i}", f"QH-NVDA-b-17000000000{i}-aaaa000{i}", "buy", 1, created=i) for i in range(1, 6)]
        s = Scenario(_nvda_state(), broker_qty=3, ledger={"qhm": {"qty": 5.0}}, history=history)
        self.assertTrue(s.run())
        self.assertEqual(s.stops, [("NVDA", 3, "sell", 163.07)])

    def test_qh_sell_fills_reduce_the_proof(self):
        history = _nvda_history() + [_order("s2", "QH-NVDA-s-1700000000009-aaaa0009", "sell", 2, created=9)]
        s = Scenario(_nvda_state(), broker_qty=1, ledger={"qhm": {"qty": 2.0}}, history=history)
        self.assertTrue(s.run())
        self.assertEqual(s.stops, [("NVDA", 1, "sell", 163.07)])       # 3 bought - 2 sold

    def test_own_resting_cover_stop_is_adopted_not_doubled(self):
        s = Scenario(_nvda_state(), book=[_resting("qh-live", "QH-NVDA-s-1700000000010-aaaa0010", 2, 163.07)])
        self.assertTrue(s.run())
        self.assertEqual(s.stops, [])
        self.assertEqual(s.pos.stop_order_id, "qh-live")

    def test_9_cancelled_stop_partly_filled_plus_add_of_one(self):
        add = SimpleNamespace(id="add-9", client_order_id="QH-NVDA-b-1700000000020-aaaa0020", symbol="NVDA",
                              side="buy", qty=1, filled_qty=1, status="filled")
        pos = _nvda_state(stop_recovery_baseline_qty=2, ambiguous_add_client_order_id=add.client_order_id,
                          ambiguous_add_baseline_qty=2)
        s = Scenario(pos, broker_qty=2, ledger={"qhm": {"qty": 2.0}},
                     canceled_stop=SimpleNamespace(id="s1", status="canceled", filled_qty=1),
                     by_coid={add.client_order_id: add})
        self.assertTrue(s.run())
        self.assertEqual(s.stops, [("NVDA", 2, "sell", 163.07)])
        self.assertEqual((s.pos.qty_filled, s.pos.state), (2, qm.HoldState.ACTIVE))
        # Stop fill reduced first, then the exact add credited: QHM's claim is survivor 1 + add 1.
        self.assertEqual([w[0] for w in s.ledger_writes], ["reduce", "increase"])
        self.assertEqual(s.qhm_ledger(), 2.0)

    def test_cancelled_stop_fill_with_unfilled_add_reduces_the_ledger(self):
        add = SimpleNamespace(id="add-9", client_order_id="QH-NVDA-b-1700000000020-aaaa0020", symbol="NVDA",
                              side="buy", qty=1, filled_qty=0, status="canceled")
        pos = _nvda_state(stop_recovery_baseline_qty=2, ambiguous_add_client_order_id=add.client_order_id,
                          ambiguous_add_baseline_qty=2)
        s = Scenario(pos, broker_qty=1, ledger={"qhm": {"qty": 2.0}},
                     canceled_stop=SimpleNamespace(id="s1", status="canceled", filled_qty=1),
                     by_coid={add.client_order_id: add})
        self.assertTrue(s.run())
        self.assertEqual(s.stops, [("NVDA", 1, "sell", 163.07)])
        self.assertEqual(s.qhm_ledger(), 1.0)


class StraySellGuardKeepsUncoveredProtection(unittest.TestCase):
    """Cold review 2026-11-09: the stray-sell guard must not strip an owner stop that protects shares QHM's stop
    does not cover; it still cancels non-protective strays and any protective excess."""

    def test_owner_stop_for_one_survives_cycles_two_and_three_without_pages(self):
        s = Scenario(_nvda_state(), book=[_resting("own1", "manual-1", 1, 160.0)])
        self.assertTrue(s.run())                                     # cycle 1: QHM stop 2, ACTIVE
        for _cycle in (2, 3):
            s.later_cycle()
        self.assertEqual(s.cancelled, [])
        self.assertIn("own1", [o.id for o in s.book])
        self.assertEqual(_action_pages(s.alerts), [])

    def test_owner_follows_the_action_page_and_the_stop_survives(self):
        s = Scenario(_nvda_state())
        self.assertTrue(s.run())                                     # QHM 2 + one ACTION page for 1
        s.book.append(_resting("own1", "manual-1", 1, 163.07))      # the owner places the stop for 1
        for _cycle in (2, 3, 4):
            s.later_cycle()
        self.assertEqual(s.cancelled, [])
        self.assertEqual(len(_action_pages(s.alerts)), 1)

    def test_owner_stop_for_three_still_survives(self):
        s = Scenario(_nvda_state(state=qm.HoldState.ACTIVE, stop_recovery_requires_qty_proof=False,
                                 stop_recovery_canceled_order_id=None, stop_recovery_add_submitted=False),
                     book=[_resting("own3", "manual-1", 3, 160.0)])
        s.later_cycle()
        self.assertEqual(s.cancelled, [])

    def _active(self, book, **kw):
        return Scenario(_nvda_state(state=qm.HoldState.ACTIVE, stop_recovery_requires_qty_proof=False,
                                    stop_recovery_canceled_order_id=None, stop_recovery_add_submitted=False,
                                    stop_order_id="qh-1"),
                        book=[_resting("qh-1", "QH-NVDA-s-1-aaaa0001", 2, 163.07)] + book, **kw)

    def test_untagged_limit_sell_above_price_is_still_cancelled(self):
        s = self._active([_resting("own1", "manual-1", 1, 160.0), _resting("lim", "manual-2", 1, None, type_="limit")])
        s.later_cycle()
        self.assertEqual(s.cancelled, ["lim"])

    def test_short_or_zero_position_keeps_no_sell_stop(self):
        for qty, side in ((-1, "short"), (0, "long")):
            with self.subTest(qty=qty):
                s = self._active([_resting("own1", "manual-1", 1, 160.0)])
                s.broker_qty = qty
                s.m.broker = SimpleNamespace(get_position=lambda _s, q=qty, sd=side: SimpleNamespace(
                    qty=q, side=sd, current_price=180.0))
                cancelled: list = []
                broker_mod = SimpleNamespace(cancel_order=lambda oid, out=cancelled: out.append(oid) or True)
                with mock.patch.dict(sys.modules, {"execution.broker": broker_mod}):
                    s.m.cancel_stray_sell_orders(open_orders=list(s.book), skip_symbols=set())
                self.assertIn("own1", cancelled)

    def test_unreadable_position_keeps_every_protective_sell(self):
        s = self._active([_resting("own1", "manual-1", 1, 160.0)])
        s.m.broker = SimpleNamespace(get_position=lambda _s: SimpleNamespace(qty="n/a", side="long",
                                                                              current_price=180.0))
        s.cancelled = []
        broker_mod = SimpleNamespace(cancel_order=lambda oid: s.cancelled.append(oid) or True)
        with mock.patch.dict(sys.modules, {"execution.broker": broker_mod}):
            s.m.cancel_stray_sell_orders(open_orders=list(s.book), skip_symbols=set())
        self.assertEqual(s.cancelled, [])

    def test_gap_that_reopens_the_same_day_pages_again(self):
        s = self._active([_resting("own1", "manual-1", 1, 160.0)])
        s.pos.cover_gap_page_key = f"{TODAY}|1"                     # the gap was paged earlier today
        s.later_cycle()                                              # owner stop closes it: gap 0, no page
        self.assertEqual(_action_pages(s.alerts), [])
        s.later_cycle()
        self.assertEqual(_action_pages(s.alerts), [])                # unchanged: quiet
        s.book = [o for o in s.book if o.id != "own1"]               # the owner cancels his stop
        s.later_cycle()
        self.assertEqual(len(_action_pages(s.alerts)), 1)            # reopened 0 -> 1: pages again
        s.later_cycle()
        self.assertEqual(len(_action_pages(s.alerts)), 1)

    def test_untagged_stop_beyond_the_uncovered_quantity_is_cancelled(self):
        s = Scenario(_nvda_state(), book=[_resting("own1", "manual-1", 1, 160.0)])
        self.assertTrue(s.run())                                     # QHM 2 + owner 1 = broker 3
        extra = _resting("own-extra", "manual-3", 1, 159.0)         # a second, newer untagged stop: excess
        extra.created_at = datetime(2026, 11, 9, 9, 30, tzinfo=qm.ET)
        s.book.append(extra)
        s.later_cycle()
        self.assertEqual(s.cancelled, ["own-extra"])

    def test_paging_with_equal_timestamps_across_pages(self):
        t = [datetime(2026, 11, 9, 9, m, tzinfo=qm.ET) for m in range(4)]

        def buy(oid, ts):
            return SimpleNamespace(id=oid, client_order_id=f"QH-NVDA-b-1-{oid}", symbol="NVDA", side="buy",
                                   qty=1, filled_qty=1, status="filled", created_at=ts, legs=None)
        # page size 3: page 1 = a, x, b; b and c share one instant across the page break
        history = [buy("a", t[0]), buy("x", t[1]), buy("b", t[2]), buy("c", t[2]), buy("d", t[3])]
        s = Scenario(_nvda_state(), history=history)
        broker_mod = SimpleNamespace(get_trading_client=lambda: s.client)
        with mock.patch.dict(sys.modules, {"execution.broker": broker_mod,
                                           "execution.manual_sale_proof": SimpleNamespace(_PAGE_SIZE=3)}):
            self.assertEqual(s.m._qhm_proved_fill_qty(s.pos), 5)
        self.assertGreater(s.client.calls, 1)

    def test_paging_fails_closed_when_one_instant_overflows_a_page(self):
        ts = datetime(2026, 11, 9, 9, 0, tzinfo=qm.ET)
        history = [SimpleNamespace(id=f"o{i}", client_order_id=f"QH-NVDA-b-1-{i}", symbol="NVDA", side="buy",
                                   qty=1, filled_qty=1, status="filled", created_at=ts, legs=None) for i in range(3)]
        s = Scenario(_nvda_state(), history=history)
        broker_mod = SimpleNamespace(get_trading_client=lambda: s.client)
        with mock.patch.dict(sys.modules, {"execution.broker": broker_mod,
                                           "execution.manual_sale_proof": SimpleNamespace(_PAGE_SIZE=2)}):
            self.assertIsNone(s.m._qhm_proved_fill_qty(s.pos))

    def test_pending_new_owner_stop_counts_as_protection(self):
        s = Scenario(_nvda_state(), book=[_resting("own1", "manual-1", 1, 160.0, status="pending_new")])
        self.assertTrue(s.run())
        self.assertEqual(s.stops, [("NVDA", 2, "sell", 163.07)])
        self.assertEqual(_action_pages(s.alerts), [])


# ── alerts (fix C / X5 / T6, test 10) and the gap watch ──────────────────────────────────────────────────────────────
class StopFailurePageReset(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(alerts, "_GTC_FAIL_PAGES", Path(self.tmp.name) / "p.json"),
                        mock.patch.object(alerts, "_ntfy", mock.Mock(return_value=True)),
                        mock.patch.object(alerts, "_NTFY_TOPIC", ""),
                        mock.patch.object(alerts, "_SLACK_WEBHOOK", "https://hooks.example/x")]
        self.slack = mock.Mock(return_value=True)
        self.patches.append(mock.patch.object(alerts, "_slack", self.slack))
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_10_failure_then_confirmed_stop_then_failure_pages_again(self):
        alerts.alert_gtc_failed("NVDA", "sell", 163.07, "wash trade")
        self.assertEqual(alerts.clear_gtc_failed("NVDA", "sell"), 1)
        alerts.alert_gtc_failed("NVDA", "sell", 163.07, "wash trade")
        self.assertEqual(self.slack.call_count, 2)

    def test_10_two_failures_without_a_confirmed_stop_page_once(self):
        alerts.alert_gtc_failed("NVDA", "sell", 163.07, "wash trade")
        alerts.alert_gtc_failed("NVDA", "sell", 163.07, "wash trade")
        self.assertEqual(self.slack.call_count, 1)

    def test_clear_only_touches_that_symbol_and_side(self):
        alerts.alert_gtc_failed("NVDA", "sell", 163.07, "x")
        alerts.alert_gtc_failed("AMZN", "sell", 200.0, "x")
        alerts.alert_gtc_failed("NVDA", "buy", 163.07, "x")
        self.assertEqual(alerts.clear_gtc_failed("NVDA", "sell"), 1)
        alerts.alert_gtc_failed("AMZN", "sell", 200.0, "x")
        alerts.alert_gtc_failed("NVDA", "buy", 163.07, "x")
        self.assertEqual(self.slack.call_count, 3)


def _active_mgr(pos, broker_qty, ledger_qhm):
    m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
    m.dry_run = False
    m._positions = {pos.symbol: pos}
    m.alerts = []
    m._alert = m.alerts.append
    m._save_state = lambda: True
    m._now_et = lambda: NOW
    m.broker = SimpleNamespace(get_position=lambda s: SimpleNamespace(qty=broker_qty["qty"], side="long",
                                                                      current_price=180.0))
    ledger = {"positions": {"NVDA": {"tiers": {"qhm": {"qty": float(ledger_qhm)}}}}}
    return m, ledger


class ConfirmedStopWiring(unittest.TestCase):
    def _reconcile(self, m, ledger, book):
        with mock.patch.object(real_guard, "load_ledger", lambda: ledger), \
                mock.patch("alerts.clear_gtc_failed") as clear:
            out = m._reconcile_stop_coverage(m._positions["NVDA"], book)
        return out, clear

    def test_confirmed_resting_stop_clears_the_page_stamp(self):
        pos = _nvda_state(state=qm.HoldState.ACTIVE, stop_recovery_requires_qty_proof=False,
                          stop_recovery_canceled_order_id=None, stop_recovery_add_submitted=False,
                          stop_order_id="qh-1")
        m, ledger = _active_mgr(pos, {"qty": 2}, 2)
        out, clear = self._reconcile(m, ledger, [_resting("qh-1", "QH-NVDA-s-1-aaaa0001", 2, 163.07)])
        self.assertEqual(out, (False, False))
        clear.assert_called_once_with("NVDA", "sell")

    def test_no_clear_when_the_stop_is_missing(self):
        pos = _nvda_state(state=qm.HoldState.ACTIVE, stop_recovery_requires_qty_proof=False,
                          stop_recovery_canceled_order_id=None, stop_recovery_add_submitted=False)
        m, ledger = _active_mgr(pos, {"qty": 2}, 2)
        _out, clear = self._reconcile(m, ledger, [])
        clear.assert_not_called()

    def test_recovery_submit_does_not_clear(self):
        with mock.patch("alerts.clear_gtc_failed") as clear:
            Scenario(_nvda_state()).run()
        clear.assert_not_called()

    def test_gap_page_repeats_only_on_change_or_a_new_day(self):
        pos = _nvda_state(state=qm.HoldState.ACTIVE, stop_recovery_requires_qty_proof=False,
                          stop_recovery_canceled_order_id=None, stop_recovery_add_submitted=False,
                          stop_order_id="qh-1", cover_gap_page_key=f"{TODAY}|1")
        held = {"qty": 3}
        m, ledger = _active_mgr(pos, held, 2)
        book = [_resting("qh-1", "QH-NVDA-s-1-aaaa0001", 2, 163.07)]
        self._reconcile(m, ledger, book)
        self.assertEqual(_action_pages(m.alerts), [])                 # same gap, same day: already paged
        held["qty"] = 4
        self._reconcile(m, ledger, book)
        self.assertEqual(len(_action_pages(m.alerts)), 1)             # the gap grew to 2
        m._now_et = lambda: datetime(2026, 11, 10, 9, 31, tzinfo=qm.ET)
        self._reconcile(m, ledger, book)
        self.assertEqual(len(_action_pages(m.alerts)), 2)             # once again on the next day
        held["qty"] = 2
        self._reconcile(m, ledger, book)
        self.assertIsNone(pos.cover_gap_page_key)                     # QHM's stop covers the whole position


if __name__ == "__main__":
    unittest.main()
