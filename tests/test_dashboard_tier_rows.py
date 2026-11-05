# ruff: noqa: E501 — long literal fixtures and expected strings (project convention)
"""Open Positions: one row per (symbol, tier) slice, each with that tier's own exit plan."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

import generate_dashboard as dashboard

_REAL_TREND_LEVEL = dashboard._swing_trend_level


@pytest.fixture(autouse=True)
def _no_bar_fetch():
    """The Swing trend-exit level reads daily bars; tests never reach the network."""
    with mock.patch.object(dashboard, "_swing_trend_level", return_value=507.25):
        yield

ET = ZoneInfo("America/New_York")
TODAY = date(2026, 11, 4)


def _order(**overrides):
    order = {
        "id": "o-1", "symbol": "MSFT", "side": "sell", "qty": "1", "filled_qty": "0",
        "type": "stop", "status": "new", "stop_price": "497.10", "limit_price": None,
        "client_order_id": "IN-MSFT-s-1791209601263-16a1b9af",
    }
    order.update(overrides)
    return order


def _msft_position():
    return {
        "symbol": "MSFT", "qty": 3.0, "side": "long", "market_value": 1650.0,
        "unrealized_pl": 0.0, "unrealized_intraday_pl": 0.0, "unrealized_plpc": 0.0,
        "current_price": 550.00, "avg_entry_price": 544.63,
    }


def _ledger():
    return {
        "MSFT": {
            "alpaca_net_qty": 3.0, "drift": 0.0,
            "tiers": {
                "day": {"qty": 2.0, "avg_cost": 548.37},
                "swing": {"qty": 1.0, "avg_cost": 526.72},
                "qhm": {"qty": 0.0, "avg_cost": 0.0},
                "forever_6": {"qty": 0.0, "avg_cost": 0.0},
            },
        }
    }


def _write_state(root: Path) -> None:
    state = root / "data" / "state"
    state.mkdir(parents=True)
    (root / "logs").mkdir()
    (state / "swing_breakout.json").write_text(json.dumps({"positions": {
        "MSFT": {"status": "open", "entry_date": "2026-10-05", "qty": 1,
                 "entry_px": 526.72, "stop_px": 497.1},
        "AAPL": {"status": "closed", "entry_date": "2026-10-15", "qty": 1, "entry_px": 345.15},
    }}))
    today = datetime.now(ET).date()
    close = datetime(today.year, today.month, today.day, 16, 0, tzinfo=ET)
    (state / "swing_trend_night.json").write_text(json.dumps({"calendar": {
        "date": today.isoformat(), "is_session": True, "close": close.isoformat(),
    }}))


def _day_lots():
    return {"DT-MSFT-b-1": {"symbol": "MSFT", "side": "long", "entry_price": 553.58,
                            "fill_qty": 2.0}}


def _render(tmp_path: Path, orders: list[dict], fills: list[dict] | None = None) -> str:
    alpaca = {
        "equity": 2600.0, "last_equity": 2600.0, "cash": 0.0, "buying_power": 100.0,
        "status": "ACTIVE", "positions": [_msft_position()], "orders": orders,
        "orders_available": True, "error": None,
        "fills": fills or [], "fills_available": fills is not None,
    }
    with (
        mock.patch.object(dashboard, "ROOT", tmp_path),
        mock.patch.object(dashboard, "LOG_DIR", tmp_path / "logs"),
        mock.patch.object(dashboard, "_load_edge_snapshot", return_value={}),
        mock.patch.object(dashboard, "_load_ownership_positions", return_value=_ledger()),
        mock.patch.object(dashboard, "_load_kill_switch_snapshot", return_value={}),
        mock.patch.object(dashboard, "_compute_spy_levels", return_value={}),
        mock.patch.object(dashboard, "compute_lifetime_stats", return_value={"total_pnl": 0.0}),
        mock.patch("execution.swing_overnight.trend_night_quantities", return_value={}),
        mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value=_day_lots()),
    ):
        return dashboard._build_html(
            alpaca, {"open": [], "closed": []}, {}, {"trades": []},
            bot_status={}, market_news={"items": []},
        )


def _position_rows(html: str) -> list[str]:
    table = html.split("<th>Float P&L</th>", 1)[1].split("Today&#39;s Trades", 1)[0]
    return [row for row in table.split("<tr>")[1:] if "MSFT" in row]


def test_two_tiers_on_one_symbol_render_two_rows_with_their_own_plans(tmp_path: Path):
    _write_state(tmp_path)
    orders = [
        _order(),
        _order(id="o-2", qty="2", stop_price="548.51",
               client_order_id="DT-MSFT-s-1793804813080-4fa943d7"),
        _order(id="o-3", qty="2", type="limit", stop_price=None, limit_price="570.00",
               client_order_id="DT-MSFT-s-1793804813081-aaaaaaaa"),
    ]
    rows = _position_rows(_render(tmp_path, orders))
    assert len(rows) == 2
    day_row = next(r for r in rows if ">Day<" in r)
    swing_row = next(r for r in rows if ">Swing<" in r)
    # quantities, each tier's own basis, slice P&L
    assert "<td>2</td>" in day_row and "$553.58" in day_row and "-$7.16" in day_row
    assert "<td>1</td>" in swing_row and "$526.72" in swing_row and "+$23.28" in swing_row
    # stops per tier, no "sh"
    assert "$548.51 (2)" in day_row and "$497.10 (1)" in swing_row
    # targets per tier
    assert "limit $570.00 (2)" in day_row and "flat by 12:58 PM PT" in day_row
    assert "no profit target (trend exit)" in swing_row and "time exit Nov 13" in swing_row
    # the 20-session-low rule is an exit level, shown under the stop with its price
    assert "trend exit: price near close &lt; $507.25" in swing_row
    assert "trend exit" not in day_row


def test_rendered_share_counts_drop_the_sh_word(tmp_path: Path):
    _write_state(tmp_path)
    day_partial_stop = _order(id="o-2", stop_price="548.51",
                              client_order_id="DT-MSFT-s-1793804813080-4fa943d7")
    html = _render(tmp_path, [_order(), day_partial_stop])
    assert not re.search(r"\(\d+(\.\d+)? sh\)", html)
    assert "remaining sh" not in html
    assert not re.search(r"\d sh</span>", html)
    assert "open <span>· (2)</span>" in html   # Day tier card: 1 open · (2)
    assert "open <span>· (1)</span>" in html   # Swing tier card: 1 open · (1)
    assert "Protect remaining (1)" in html     # Day slice: 1 of 2 shares stopped


def _plans(**overrides):
    plans = {
        "breakout": {}, "trend_night": {}, "trend_night_symbol": "QQQM", "day": {},
        "day_promoting": set(), "day_flat_pt": "12:58 PM PT", "qhm": {}, "qhm_trim": {},
        "ledger": {},
    }
    plans.update(overrides)
    return plans


def test_each_tier_target_text_comes_from_its_own_state():
    trend = dashboard._tier_slice_plan(
        "swing", "QQQM", 2.0, _plans(trend_night={"QQQM": 2.0}), {}, None, TODAY)
    assert trend == (None, "sells at next open")

    basis, day = dashboard._tier_slice_plan(
        "day", "AAPL", 4.0,
        _plans(day={"AAPL": [{"entry_price": 352.58, "fill_qty": 4.0}]}), {}, None, TODAY)
    assert basis == 352.58 and day == "flat by 12:58 PM PT"
    _, promoting = dashboard._tier_slice_plan(
        "day", "AAPL", 4.0, _plans(day_promoting={"AAPL"}), {}, None, TODAY)
    assert promoting == "moves to Swing at close"
    _, no_clock = dashboard._tier_slice_plan(
        "day", "AAPL", 4.0, _plans(day_flat_pt=None), {}, None, TODAY)
    assert no_clock == "flat 2 min before close"

    qhm = {"NVDA": {"qty_filled": 2, "avg_entry_price": 221.075, "entry_day": "2026-10-13"},
           "GE": {"qty_filled": 1, "avg_entry_price": 302.50, "entry_day": "2026-09-29"}}
    trims = {"NVDA": {"gate_date": "2026-11-17", "tier1": "none", "tier2": "none",
                      "t1_target_pct": 0.11133849827023341}}
    basis, nvda = dashboard._tier_slice_plan(
        "qhm", "NVDA", 2.0, _plans(qhm=qhm, qhm_trim=trims), {}, None, TODAY)
    assert basis == 221.075
    assert nvda == "trim 1 at $245.69 (+11.1%) before earnings Nov 17 · max-hold exit Jan 12 2027"
    _, ge = dashboard._tier_slice_plan(
        "qhm", "GE", 1.0, _plans(qhm=qhm, qhm_trim=trims), {}, None, TODAY)
    assert ge == "max-hold exit Dec 29"

    ledger = {"TSLA": {"tiers": {"forever6": {"qty": 4.0, "avg_cost": 30.0}}}}
    basis, f6 = dashboard._tier_slice_plan(
        "forever_6", "TSLA", 4.0, _plans(ledger=ledger), {}, None, TODAY)
    assert basis == 30.0 and f6 == "never sells all · trims 1 at $300.00 (10× cost)"
    _, f6_small = dashboard._tier_slice_plan(
        "forever_6", "TSLA", 2.0, _plans(ledger=ledger), {}, None, TODAY)
    assert f6_small == "never sells all · no trim below 4 shares"


def test_swing_time_exit_falls_back_to_rule_text_past_the_holiday_calendar():
    plans = _plans(breakout={"V": {"status": "open", "entry_date": "2026-12-15", "qty": 1,
                                   "entry_px": 420.0}})
    basis, text = dashboard._tier_slice_plan("swing", "V", 1.0, plans, {}, None, TODAY)
    assert basis == 420.0
    assert text == "no profit target (trend exit) · time exit on session 30"


def test_missing_or_unreadable_state_shows_dash():
    unknown = dashboard._UNKNOWN
    assert dashboard._tier_slice_plan(
        "swing", "MSFT", 1.0, _plans(breakout=unknown, trend_night=unknown), {}, None, TODAY
    ) == (None, "—")
    assert dashboard._tier_slice_plan(
        "day", "MSFT", 1.0, _plans(day=unknown), {}, None, TODAY) == (None, "—")
    assert dashboard._tier_slice_plan(
        "qhm", "GE", 1.0, _plans(qhm=unknown), {}, None, TODAY) == (None, "—")
    assert dashboard._tier_slice_plan(
        "unattributed", "XYZ", 1.0, _plans(), {}, None, TODAY) == (None, "—")


def test_a_failing_tier_lookup_never_stops_the_page(tmp_path: Path):
    _write_state(tmp_path)
    with mock.patch.object(dashboard, "_tier_slice_plan", side_effect=RuntimeError("boom")):
        rows = _position_rows(_render(tmp_path, [_order()]))
    assert len(rows) == 2
    for row in rows:
        assert '<td style="color:#30d158">—</td>' in row
        assert "broker avg" in row        # basis unknown → broker average, marked


def test_missing_state_files_render_without_inventing_numbers(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    rows = _position_rows(_render(tmp_path, [_order()]))
    swing_row = next(r for r in rows if ">Swing<" in r)
    assert '<td style="color:#30d158">—</td>' in swing_row
    assert "broker avg" in swing_row


# ── 2026-11-04 owner revision: bracket legs, today's trades ──────────────────

def _msft_bracket_legs():
    """Exactly today's MSFT Day bracket: parent buy 2 filled; children (bare UUID ids)
    sell LIMIT $576.92 status new, sell STOP $548.51 status held."""
    parent = "DT-MSFT-b-1793804813080-4fa943d7"
    common = {"symbol": "MSFT", "side": "sell", "qty": "2", "filled_qty": "0",
              "owner_client_order_id": parent, "owner_order_class": "bracket",
              "owner_status": "filled", "order_class": "bracket"}
    limit = {**common, "id": "leg-tp", "type": "limit", "status": "new",
             "stop_price": None, "limit_price": "576.92",
             "client_order_id": "4483dc34-138f-4bc4-8e93-dd5abf3cd631"}
    stop = {**common, "id": "leg-sl", "type": "stop", "status": "held",
            "stop_price": "548.51", "limit_price": None,
            "client_order_id": "ffc92b00-f339-41fd-90b2-f6a958fbfebe"}
    return limit, stop


def test_held_bracket_stop_of_a_filled_entry_is_live_protection():
    limit, stop = _msft_bracket_legs()
    fact = dashboard._position_protection(
        _msft_position(), [("day", 2.0), ("swing", 1.0)], [limit, stop, _order()],
        orders_available=True,
    )
    day = next(f for f in fact if f["tier"] == "day")
    assert day["status"] == "PROTECTED"
    assert day["stops"] == [(548.51, 2.0)]
    assert day["targets"] == [(576.92, 2.0)]
    # the same held leg under an UNFILLED bracket entry is not protection
    unfilled = {**stop, "owner_status": "new"}
    day2 = dashboard._position_protection(
        _msft_position(), [("day", 2.0), ("swing", 1.0)], [unfilled, _order()],
        orders_available=True,
    )[0]
    assert day2["status"] == "MISSING"


def test_bracket_limit_leg_is_the_day_rows_target_and_stop_is_not_missing(tmp_path: Path):
    _write_state(tmp_path)
    limit, stop = _msft_bracket_legs()
    html = _render(tmp_path, [limit, stop, _order()])
    rows = _position_rows(html)
    day_row = next(r for r in rows if ">Day<" in r)
    assert "limit $576.92 (2)" in day_row
    assert "$548.51 (2)" in day_row
    assert "MISSING" not in day_row and "Place broker stop now" not in day_row
    assert "issue" not in html.split("<span class=\"pt\">Open Positions</span>", 1)[1].split("</div>", 1)[0]


def test_missing_stop_shows_status_and_issue_badge(tmp_path: Path):
    _write_state(tmp_path)
    html = _render(tmp_path, [_order()])                 # Day slice has no stop at all
    day_row = next(r for r in _position_rows(html) if ">Day<" in r)
    assert "MISSING" in day_row and "Place broker stop now" in day_row
    head = html.split('<span class="pt">Open Positions</span>', 1)[1].split("</div>", 1)[0]
    assert "1 issue" in head


def _ev(ts: datetime, **fields) -> str:
    return json.dumps({"ts": ts.isoformat(), **fields})


def test_todays_trades_lists_every_recorded_fill_newest_first(tmp_path: Path):
    _write_state(tmp_path)
    pt = ZoneInfo("America/Los_Angeles")
    today = datetime.now(pt).date()
    at = lambda h, m: datetime(today.year, today.month, today.day, h, m, tzinfo=pt)  # noqa: E731
    yesterday = at(13, 0) - timedelta(days=1)
    lines = [
        _ev(yesterday, event="fill", symbol="QQQM", price=282.2, tier="swing",
            strategy="trend_night", side="buy", qty=2),
        _ev(at(6, 36), event="exit", symbol="QQQM", price=284.04, size=2, tier="swing",
            strategy="trend_night", pnl=3.68),
        _ev(at(6, 36), event="fill", symbol="QQQM", price=284.04, tier="swing",
            strategy="trend_night", side="sell", qty=1),
        _ev(at(6, 40), event="delta_shadow", symbol="SPY"),
        _ev(at(7, 3), event="exit", symbol="XOM", price=171.86, size=3, tier="intraday",
            exit_reason="protective_stop", realized_pnl=-34.98,
            trade_id="IN-XOM-b-1793023958010-778d0796"),
        _ev(at(7, 6), event="entry", symbol="MSFT", price=553.58, size=2, tier="daytrade",
            direction="long", trade_id="DT-MSFT-b-1793804813080-4fa943d7"),
        _ev(at(10, 17), event="exit", symbol="GE", price=None, size=1,
            exit_reason="external_close_detected", data_source="qhm_external_close"),
    ]
    (tmp_path / "logs" / "trade_events.jsonl").write_text("\n".join(lines) + "\n")
    html = _render(tmp_path, [_order()])
    table = html.split("Today&#39;s Trades", 1)[1].split("</table>", 1)[0]
    rows = table.split("<tr>")[2:]               # skip the header row
    assert [re.findall(r'font-weight:700">([A-Z.]+)<', r)[0] for r in rows] == [
        "GE", "MSFT", "XOM", "QQQM"]
    ge, msft, xom, qqqm = rows
    assert ">QHM<" in ge and ">SELL<" in ge and "<td>—</td>" in ge and "closed outside the bot" in ge
    assert ">Day<" in msft and ">BUY<" in msft and "(2)" in msft and "$553.58" in msft
    assert ">Swing<" in xom and "(3)" in xom and "$171.86" in xom and "-$34.98" in xom
    assert "protective stop" in xom                       # closed: XOM is not in the top table
    assert "XOM" not in "".join(_position_rows(html))
    assert ">Swing<" in qqqm and "+$3.68" in qqqm and "next open" in qqqm
    assert "4 fills · from tier records (broker fills unavailable)" in html
    assert " sh)" not in table


def test_todays_trades_with_no_log_says_so(tmp_path: Path):
    _write_state(tmp_path)
    html = _render(tmp_path, [_order()])
    assert "No fills recorded today" in html


# ── broker fills as the source of Today's Trades ─────────────────────────────

def _fill(when: datetime, symbol: str, side: str, qty: str, price: str, order_id: str, kind="fill"):
    return {"activity_type": "FILL", "id": f"{order_id}-{when.timestamp()}", "type": kind,
            "transaction_time": when.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
            "symbol": symbol, "side": side, "qty": qty, "price": price, "order_id": order_id}


def _todays_tier_log(tmp_path: Path, at) -> None:
    lines = [
        _ev(at(7, 3), event="exit", symbol="XOM", price=171.86, size=3, tier="intraday",
            exit_reason="protective_stop", realized_pnl=-34.98,
            trade_id="IN-XOM-b-1793023958010-778d0796"),
        _ev(at(10, 17), event="exit", symbol="GE", price=None, size=1,
            exit_reason="external_close_detected", data_source="qhm_external_close"),
    ]
    (tmp_path / "logs" / "trade_events.jsonl").write_text("\n".join(lines) + "\n")
    (tmp_path / "data" / "state" / "quarterly_holds.json").write_text(json.dumps({
        "GE": {"state": "ACTIVE", "qty_filled": 1, "avg_entry_price": 302.50},
        "NVDA": {"state": "ACTIVE", "qty_filled": 2, "avg_entry_price": 221.075},
    }))


def test_broker_fills_drive_todays_trades_with_tier_pnl_and_reason_joined(tmp_path: Path):
    _write_state(tmp_path)
    pt = ZoneInfo("America/Los_Angeles")
    today = datetime.now(pt).date()
    at = lambda h, m, s=0: datetime(today.year, today.month, today.day, h, m, s, tzinfo=pt)  # noqa: E731
    _todays_tier_log(tmp_path, at)
    fills = [
        _fill(at(10, 10), "GE", "sell", "1", "330.50", "ge-sale"),
        _fill(at(9, 0), "NVDA", "buy", "1", "200.00", "nvda-add"),     # QHM buy: no tier event
        _fill(at(8, 0), "ZZZ", "buy", "1", "10.00", "unknown"),
        _fill(at(6, 33, 58), "XOM", "sell", "2", "171.70", "xom-stop"),
        _fill(at(6, 32, 37), "XOM", "sell", "1", "172.18", "xom-stop", kind="partial_fill"),
    ]
    html = _render(tmp_path, [_order()], fills=fills)
    table = html.split("Today&#39;s Trades", 1)[1].split("</table>", 1)[0]
    rows = table.split("<tr>")[2:]
    assert [re.findall(r'font-weight:700">([A-Z.]+)<', r)[0] for r in rows] == [
        "GE", "NVDA", "ZZZ", "XOM"]
    ge, nvda, zzz, xom = rows
    # GE: the real sale price from the broker, QHM tier, P&L vs the hold's own cost
    assert ">QHM<" in ge and "$330.50" in ge and "+$28.00" in ge and "closed outside the bot" in ge
    assert ">QHM<" in nvda and ">BUY<" in nvda and "(1)" in nvda and "$200.00" in nvda
    assert ">Unattributed<" in zzz
    # XOM: two partial fills of one order → one row, total qty, volume-weighted price
    assert ">Swing<" in xom and "(3)" in xom and "$171.86" in xom and "-$34.98" in xom
    assert "protective stop" in xom and "6:33 AM PT" in xom
    assert "4 orders filled · broker fills" in html


def test_broker_fill_pnl_uses_each_fills_own_price():
    pt = ZoneInfo("America/Los_Angeles")
    t = datetime(2026, 11, 4, 6, 36, tzinfo=pt)
    events = [{"event": "exit", "symbol": "QQQM", "price": 284.04, "size": 2, "tier": "swing",
               "strategy": "trend_night", "pnl": 3.68, "_ts": t}]
    fills = [_fill(t, "QQQM", "sell", "1", "283.95", "a"),
             _fill(t, "QQQM", "sell", "1", "284.13", "b")]
    rows = dashboard._broker_trade_rows(fills, events, {}, {}, {})
    assert sorted(round(r["pnl"], 2) for r in rows) == [1.75, 1.93]
    assert {r["reason"] for r in rows} == {"next open"} and {r["tier"] for r in rows} == {"swing"}


def test_order_id_known_locally_sets_the_tier_before_any_time_match():
    pt = ZoneInfo("America/Los_Angeles")
    t = datetime(2026, 11, 4, 7, 6, tzinfo=pt)
    fills = [_fill(t, "MSFT", "buy", "1", "553.58", "c37fa53d", kind="partial_fill"),
             _fill(t, "MSFT", "buy", "1", "553.58", "c37fa53d")]
    rows = dashboard._broker_trade_rows(fills, [], {"c37fa53d": "day"}, {"MSFT": "swing"}, {})
    assert len(rows) == 1 and rows[0]["tier"] == "day" and rows[0]["qty"] == 2.0
    assert rows[0]["pnl"] is None


def test_fills_fetch_pages_until_a_short_page():
    calls = []

    def get(path):
        calls.append(path)
        n = dashboard._FILL_PAGE_SIZE if len(calls) == 1 else 3
        return [{"id": f"p{len(calls)}-{i}"} for i in range(n)]

    fills = dashboard._fetch_today_fills(get)
    assert len(fills) == dashboard._FILL_PAGE_SIZE + 3
    assert "page_token" not in calls[0]
    assert f"page_token=p1-{dashboard._FILL_PAGE_SIZE - 1}" in calls[1]
    assert "/v2/account/activities/FILL?after=" in calls[0]


def test_fills_fetch_failure_keeps_account_data_and_falls_back(tmp_path: Path):
    import io

    def fake_urlopen(req, context=None, timeout=None):
        url = req.full_url
        if "/v2/account/activities/FILL" in url:
            raise OSError("fills endpoint down")
        body = {"/v2/account": {"equity": "2600"}, "/v2/positions": [], "/v2/orders": []}
        key = next(k for k in ("/v2/account", "/v2/positions", "/v2/orders")
                   if url.split("paper-api.alpaca.markets", 1)[1].split("?", 1)[0] == k)
        return io.BytesIO(json.dumps(body[key]).encode())

    with (
        mock.patch.dict("os.environ", {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}),
        mock.patch("dotenv.load_dotenv"),
        mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        alpaca = dashboard._load_alpaca()
    assert alpaca["error"] is None and alpaca["equity"] == 2600.0
    assert alpaca["fills_available"] is False and alpaca["fills"] == []

    _write_state(tmp_path)
    html = _render(tmp_path, [_order()], fills=None)
    assert "from tier records (broker fills unavailable)" in html


def test_a_broken_fill_record_never_stops_the_page(tmp_path: Path):
    _write_state(tmp_path)
    with mock.patch.object(dashboard, "_broker_trade_rows", side_effect=RuntimeError("boom")):
        html = _render(tmp_path, [_order()], fills=[{"bad": True}])
    assert "from tier records (broker fills unavailable)" in html
    assert "<th>Float P&L</th>" in html


def test_broker_timestamps_with_five_fraction_digits_are_read():
    fills = [
        {"transaction_time": "2026-11-04T14:32:37.37442Z", "symbol": "XOM", "side": "sell",
         "qty": "1", "price": "172.18", "order_id": "be404bfa"},
        {"transaction_time": "2026-11-04T14:33:58.151712Z", "symbol": "XOM", "side": "sell",
         "qty": "2", "price": "171.7", "order_id": "be404bfa"},
    ]
    rows = dashboard._broker_trade_rows(fills, [], {}, {}, {})
    assert len(rows) == 1 and rows[0]["qty"] == 3.0 and round(rows[0]["price"], 2) == 171.86


def _alpaca_with(open_orders, closed_orders, closed_error=None):
    import io

    def fake_urlopen(req, context=None, timeout=None):
        url = req.full_url
        path = url.split("paper-api.alpaca.markets", 1)[1].split("?", 1)[0]
        if "/v2/account/activities/FILL" in url:
            return io.BytesIO(b"[]")
        if path == "/v2/orders" and "status=closed" in url:
            if closed_error:
                raise closed_error
            return io.BytesIO(json.dumps(closed_orders).encode())
        body = {"/v2/account": {"equity": "2600"}, "/v2/positions": [_msft_position()],
                "/v2/orders": open_orders}
        return io.BytesIO(json.dumps(body[path]).encode())

    with (
        mock.patch.dict("os.environ", {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}),
        mock.patch("dotenv.load_dotenv"),
        mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        return dashboard._load_alpaca()


def _day_bracket(parent_status="filled"):
    tp = {"id": "tp-1", "symbol": "MSFT", "side": "sell", "qty": "2", "filled_qty": "0", "type": "limit",
          "status": "new", "limit_price": "576.92", "stop_price": None, "order_class": "bracket",
          "client_order_id": "42f86e21-519d"}
    sl = {"id": "sl-1", "symbol": "MSFT", "side": "sell", "qty": "2", "filled_qty": "0", "type": "stop",
          "status": "held", "limit_price": None, "stop_price": "548.51", "order_class": "bracket",
          "client_order_id": "ffc92b00-aaaa"}
    parent = {"id": "par-1", "symbol": "MSFT", "side": "buy", "qty": "2", "filled_qty": "2", "type": "limit",
              "status": parent_status, "limit_price": "557.98", "stop_price": None, "order_class": "bracket",
              "client_order_id": "DT-MSFT-b-1793804813080-4fa943d7", "legs": [tp, sl]}
    return tp, sl, parent


def test_held_stop_of_a_filled_bracket_is_read_from_closed_orders():
    """2026-11-04 MSFT Day: the filled parent leaves status=open and Alpaca lists only the take-profit
    leg; the held $548.51 stop must still count as the lot's protection."""
    tp, _sl, parent = _day_bracket()
    alpaca = _alpaca_with([tp], [parent])
    ids = [o["id"] for o in alpaca["orders"]]
    assert ids.count("tp-1") == 1 and "par-1" not in ids
    stop = next(o for o in alpaca["orders"] if o["id"] == "sl-1")
    assert stop["status"] == "held" and stop["owner_status"] == "filled"
    assert stop["owner_order_class"] == "bracket"
    assert stop["owner_client_order_id"] == "DT-MSFT-b-1793804813080-4fa943d7"


def test_held_stop_of_an_unfilled_bracket_is_not_added():
    tp, _sl, parent = _day_bracket(parent_status="canceled")
    alpaca = _alpaca_with([tp], [parent])
    assert "sl-1" not in [o["id"] for o in alpaca["orders"]]


def test_closed_orders_failure_keeps_the_open_list():
    tp, _sl, _parent = _day_bracket()
    alpaca = _alpaca_with([tp], [], closed_error=OSError("down"))
    assert alpaca["error"] is None and [o["id"] for o in alpaca["orders"]] == ["tp-1"]
    assert alpaca["orders_available"] is True


def test_swing_trend_level_failure_returns_none_and_is_not_cached():
    dashboard._TREND_LEVEL_CACHE.clear()
    dashboard._TREND_LEVEL_MISS.clear()
    with mock.patch("data.fetcher.fetch_closed_bars", side_effect=RuntimeError("no bars")):
        assert _REAL_TREND_LEVEL("V", "2026-11-04") is None
        assert _REAL_TREND_LEVEL("V", "2026-11-04") is None      # no re-fetch inside the retry window
    assert dashboard._TREND_LEVEL_CACHE == {}


def test_swing_trend_level_uses_the_prior_20_session_low_and_caches():
    import pandas as pd

    dashboard._TREND_LEVEL_CACHE.clear()
    dashboard._TREND_LEVEL_MISS.clear()
    df = pd.DataFrame({"low": [float(500 + i) for i in range(30)]})
    bars = mock.Mock(df=df)
    with mock.patch("data.fetcher.fetch_closed_bars", return_value=bars) as fetch:
        assert _REAL_TREND_LEVEL("V", "2026-11-04") == 510.0     # lowest of the last 20 lows
        assert _REAL_TREND_LEVEL("V", "2026-11-04") == 510.0
    assert fetch.call_count == 1
