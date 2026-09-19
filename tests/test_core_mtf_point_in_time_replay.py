from datetime import datetime, timedelta, timezone

from research.core_mtf_point_in_time_replay import OHLCBar, ShortCandidate, completed_bars, evaluate_short


UTC = timezone.utc


def bar(hour, open_, high, low, close):
    return OHLCBar(datetime(2026, 9, 1, hour, tzinfo=UTC), open_, high, low, close)


def candidate(stop=105, target=95):
    return ShortCandidate(datetime(2026, 9, 1, 10, tzinfo=UTC), "TEST", stop, target,
                          timedelta(hours=2), "unit")


def test_completed_bar_excludes_forming_bar():
    bars = [bar(9, 100, 101, 99, 100), bar(10, 100, 101, 99, 100)]
    assert completed_bars(bars, as_of=datetime(2026, 9, 1, 10, 15, tzinfo=UTC),
                          duration=timedelta(minutes=15)) == bars
    assert completed_bars(bars, as_of=datetime(2026, 9, 1, 10, 14, tzinfo=UTC),
                          duration=timedelta(minutes=15)) == [bars[0]]


def test_next_bar_open_is_entry_not_decision_bar_close():
    result = evaluate_short(candidate(), [bar(10, 100, 101, 99, 100), bar(11, 101, 102, 100, 101)],
                            round_trip_cost_bps=0)
    assert result["entry_price"] == 101


def test_ambiguous_target_stop_bar_is_adverse_stop():
    result = evaluate_short(candidate(), [bar(11, 100, 106, 94, 100)], round_trip_cost_bps=0)
    assert result["exit_reason"] == "stop"
    assert result["ambiguous_intrabar"] is True


def test_stop_gap_uses_executable_open():
    result = evaluate_short(candidate(), [bar(11, 100, 101, 99, 100), bar(12, 108, 109, 107, 108)], round_trip_cost_bps=0)
    assert result["exit_price"] == 108
