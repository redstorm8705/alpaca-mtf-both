"""Tests for conviction linear spline — _LINEAR_SCORE_MIN/MAX expansion in main.py.

Validates boundary conditions and interpolation accuracy for the proposed change:
  _LINEAR_SCORE_MIN: 10 → 9
  _LINEAR_SCORE_MAX: 11 → 12
  _pct_min: BUCKET_B_ALLOCATION_PCT * 0.5 → 0.0

The spline formula in entry_logic.py:
  fraction = (score - MIN) / (MAX - MIN)
  pct = pct_min + fraction * (pct_max - pct_min)
  dollar_cap = portfolio_value * pct

Expected allocation fractions (proposed):
  score < 9   → 0.0 (skip via score < _LINEAR_SCORE_MIN gate)
  score == 9  → 0.000  (fraction = 0/3 = 0.000)
  score == 10 → 0.333  (fraction = 1/3 = 0.333)
  score == 11 → 0.667  (fraction = 2/3 = 0.667)
  score >= 12 → 1.000  (fraction = 3/3 = 1.000)
"""
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BUCKET_B_ALLOCATION_PCT = 0.95  # mirror config.py paper profile
PORTFOLIO_VALUE = 2500.0

# Replicate the proposed formula from entry_logic.py
LINEAR_SCORE_MIN_PROPOSED = 9
LINEAR_SCORE_MAX_PROPOSED = 12
PCT_MIN_PROPOSED = 0.0
PCT_MAX_PROPOSED = BUCKET_B_ALLOCATION_PCT


def _compute_dollar_cap_proposed(score: int, portfolio_value: float = PORTFOLIO_VALUE) -> float:
    """Replicate the proposed linear sizing formula from entry_logic.py."""
    pct_min = PCT_MIN_PROPOSED
    pct_max = PCT_MAX_PROPOSED
    if score < LINEAR_SCORE_MIN_PROPOSED:
        return 0.0
    elif score >= LINEAR_SCORE_MAX_PROPOSED:
        return portfolio_value * pct_max
    else:
        fraction = (score - LINEAR_SCORE_MIN_PROPOSED) / (LINEAR_SCORE_MAX_PROPOSED - LINEAR_SCORE_MIN_PROPOSED)
        pct = pct_min + fraction * (pct_max - pct_min)
        return portfolio_value * pct


# --- Boundary tests ---

def test_score_below_min_is_zero():
    assert _compute_dollar_cap_proposed(8) == 0.0
    assert _compute_dollar_cap_proposed(0) == 0.0
    assert _compute_dollar_cap_proposed(-1) == 0.0


def test_score_at_min_is_zero():
    """score == MIN: fraction = 0 → pct = 0.0 → dollar_cap = 0."""
    cap = _compute_dollar_cap_proposed(9)
    assert cap == 0.0, f"score=9 should be 0.0, got {cap}"


def test_score_10_is_one_third():
    """score == 10: fraction = 1/3 → pct = 0.317 → dollar_cap ≈ $791.67."""
    cap = _compute_dollar_cap_proposed(10)
    expected = PORTFOLIO_VALUE * (1 / 3) * BUCKET_B_ALLOCATION_PCT
    assert abs(cap - expected) < 0.01, f"score=10 cap {cap:.2f} != expected {expected:.2f}"


def test_score_11_is_two_thirds():
    """score == 11: fraction = 2/3 → pct = 0.633 → dollar_cap ≈ $1583.33."""
    cap = _compute_dollar_cap_proposed(11)
    expected = PORTFOLIO_VALUE * (2 / 3) * BUCKET_B_ALLOCATION_PCT
    assert abs(cap - expected) < 0.01, f"score=11 cap {cap:.2f} != expected {expected:.2f}"


def test_score_at_max_is_full():
    """score >= MAX: dollar_cap = portfolio_value * BUCKET_B_ALLOCATION_PCT."""
    cap = _compute_dollar_cap_proposed(12)
    expected = PORTFOLIO_VALUE * BUCKET_B_ALLOCATION_PCT
    assert abs(cap - expected) < 0.01, f"score=12 cap {cap:.2f} != full {expected:.2f}"


def test_score_above_max_is_full():
    """score > MAX: capped at full allocation (not extrapolated)."""
    cap_12 = _compute_dollar_cap_proposed(12)
    cap_13 = _compute_dollar_cap_proposed(13)
    assert abs(cap_12 - cap_13) < 0.01, "score 13 should equal score 12 (capped at max)"


def test_monotonic_increase():
    """Each score step must produce a strictly non-decreasing allocation."""
    caps = [_compute_dollar_cap_proposed(s) for s in range(8, 14)]
    for i in range(len(caps) - 1):
        assert caps[i] <= caps[i + 1], (
            f"Non-monotonic at score {8 + i}: {caps[i]:.2f} > {caps[i + 1]:.2f}"
        )


def test_no_int_truncation_before_floor():
    """Intermediate pct value must not truncate to 0 via int() for score 10-11."""
    for score in (10, 11):
        cap = _compute_dollar_cap_proposed(score)
        assert cap > 0.0, f"score={score} produced zero dollar_cap — int() truncation risk"


# --- Regression: verify proposed > current at score 12 (current caps at 95% from score 11) ---

# Current formula (before this change)
LINEAR_SCORE_MIN_CURRENT = 10
LINEAR_SCORE_MAX_CURRENT = 11
PCT_MIN_CURRENT = BUCKET_B_ALLOCATION_PCT * 0.5  # 47.5%


def _compute_dollar_cap_current(score: int, portfolio_value: float = PORTFOLIO_VALUE) -> float:
    pct_min = PCT_MIN_CURRENT
    pct_max = PCT_MAX_PROPOSED
    if score < LINEAR_SCORE_MIN_CURRENT:
        return 0.0
    elif score >= LINEAR_SCORE_MAX_CURRENT:
        return portfolio_value * pct_max
    else:
        fraction = (score - LINEAR_SCORE_MIN_CURRENT) / (LINEAR_SCORE_MAX_CURRENT - LINEAR_SCORE_MIN_CURRENT)
        pct = pct_min + fraction * (pct_max - pct_min)
        return portfolio_value * pct


def test_score_9_was_zero_current():
    """Current formula: score 9 < MIN=10 → 0.0 allocation (skip)."""
    assert _compute_dollar_cap_current(9) == 0.0


def test_score_10_current_is_half():
    """Current formula: score 10 = MIN → fraction=0 → pct=47.5%."""
    cap = _compute_dollar_cap_current(10)
    expected = PORTFOLIO_VALUE * PCT_MIN_CURRENT
    assert abs(cap - expected) < 0.01, f"Current score=10 cap {cap:.2f} != {expected:.2f}"


def test_proposed_reduces_score_10_allocation():
    """Proposed changes score-10 allocation from 47.5% → 31.7% (smoother gradient)."""
    current = _compute_dollar_cap_current(10)
    proposed = _compute_dollar_cap_proposed(10)
    assert proposed < current, (
        f"Proposed score-10 ({proposed:.2f}) should be less than current ({current:.2f})"
    )


def test_proposed_reduces_score_11_allocation():
    """Proposed changes score-11 allocation from 95% → 63.3% (extends gradient to 12)."""
    current = _compute_dollar_cap_current(11)
    proposed = _compute_dollar_cap_proposed(11)
    assert proposed < current, (
        f"Proposed score-11 ({proposed:.2f}) should be less than current ({current:.2f})"
    )
