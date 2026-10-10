import pytest

from tier_names import (
    TIER_IDS,
    canonical_tier,
    legacy_storage_tier,
    tier_label,
)


def test_exactly_four_canonical_tiers():
    assert TIER_IDS == ("day", "swing", "qhm", "forever_6")


@pytest.mark.parametrize(
    ("raw", "canonical", "label"),
    [
        ("day", "day", "Day"),
        ("daytrade", "day", "Day"),
        ("swing", "swing", "Swing"),
        ("intraday", "swing", "Swing"),
        ("qhm", "qhm", "QHM"),
        ("forever_6", "forever_6", "F6"),
        ("forever6", "forever_6", "F6"),
    ],
)
def test_legacy_keys_are_read_as_canonical(raw, canonical, label):
    assert canonical_tier(raw) == canonical
    assert tier_label(raw) == label


def test_unknown_or_non_string_tier_is_rejected():
    with pytest.raises(ValueError):
        canonical_tier("core_mtf")
    with pytest.raises(ValueError):
        canonical_tier(None)


def test_v1_storage_adapter_is_explicit_and_reversible():
    assert {tier: legacy_storage_tier(tier) for tier in TIER_IDS} == {
        "day": "daytrade",
        "swing": "intraday",
        "qhm": "qhm",
        "forever_6": "forever6",
    }
