#!/usr/bin/env python3
# ruff: noqa: E501  — formulas + citations run long (project convention)
"""
research/deflated_sharpe.py — Probabilistic (PSR) and Deflated (DSR) Sharpe Ratio.

WHY (learning-loop mandate + tweet-queue TW-1 + whitespace eval #1, 2026-09-07; design +
BGGN pass: logs/design_records/deflated_sharpe_2026-09-07.md): the bot validates MULTIPLE shadow
signals and today reports only a NAIVE Sharpe (backtest_12pt.py:471 = mean/std·√trades_per_year),
which says nothing about whether a "winning" shadow is a real edge or the luckiest of N noise draws.
This answers that honestly:

  - PSR  (Bailey & López de Prado 2012): P(true SR > SR*), correcting the observed Sharpe for SAMPLE
    LENGTH and NON-NORMALITY (skew γ3 + Pearson kurtosis γ4).
  - DSR  (Bailey & López de Prado 2014): PSR with SR* = the EXPECTED MAXIMUM Sharpe under the null
    across N independent TRIALS — i.e. deflated for MULTIPLE TESTING. DSR ≥ confidence means the
    Sharpe is distinguishable from the best of N noise draws.

PURE STATS — numpy/scipy only; READ-ONLY (computes and returns; never trades/sizes/writes state).
Off the RTH risk path. `sharpe_not_noise` is decision-SUPPORT feeding a human/board gate, never an
auto-trigger, and it is NECESSARY-NOT-SUFFICIENT: costs/leakage/OOS still gate any live flip.

BOARD-REQUIRED CONTRACTS (LdP + Thorp + Gro + GAI, 2026-09-07):
  - FREQUENCY: the public API takes a per-observation RETURN SERIES and computes the per-observation
    SR internally. It NEVER accepts a pre-annualized Sharpe scalar (that would corrupt the √(n-1)/
    moment math). Brick-2 callers must pass the raw R series, not backtest_12pt's annualized Sharpe.
  - HARD SMALL-SAMPLE FLOOR: below _MIN_N_HARD (=KELLY_MIN_SAMPLE_SIZE=30) observations, PSR/DSR are
    NaN and `sharpe_not_noise` is False regardless of the point estimate (Thorp: never bet an
    under-powered estimate). Below _MIN_N_MOMENTS (50) the skew/kurtosis SEs are large, so we drop
    them and fall back to a normal-Sharpe PSR rather than feed a noisy γ4.
  - σ_trials REQUIRED for DSR: if the cross-trial Sharpe dispersion is not supplied, DSR is NaN (no
    fallback to the single-strategy SR standard error — that is a DIFFERENT statistic and silently
    over-states DSR). Brick 2 supplies the measured std of the shadow signals' Sharpes.
  - `sr_standard_error` (the SR estimator SE) is exposed so brick 2 can turn "significant" into a
    Kelly sizing HAIRCUT rather than a binary flip.

HONEST LIMITS: PSR corrects non-normality, NOT autocorrelation — overlapping-hold return series are
serially dependent, so √(n-1) overstates the effective sample; treat n as an upper bound there.
`n_trials` must count EVERY variant ever tried (not just the live winner) or DSR over-states
significance; and correlated trials mean the effective N < raw N (using raw N is CONSERVATIVE).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
from scipy import stats

_EULER = 0.5772156649015329          # Euler–Mascheroni constant γ
_MIN_N_HARD = 30                     # hard floor: below this, PSR/DSR NaN + sharpe_not_noise False (= KELLY_MIN_SAMPLE_SIZE)
_MIN_N_MOMENTS = 50                  # below this, drop skew/kurtosis (their SEs are large) → normal-Sharpe PSR


@dataclass
class DSRResult:
    n: int                            # number of return observations
    sharpe: float                     # observed PER-OBSERVATION Sharpe (NOT annualized)
    skew: float                       # sample skewness γ3 (0.0 when moments dropped)
    kurtosis: float                   # sample Pearson kurtosis γ4 (normal = 3.0; 3.0 when moments dropped)
    sr_standard_error: float          # SE of the SR estimator (√(V/(n-1))) — brick-2 Kelly sizing haircut
    psr: float                        # P(true SR > 0) in [0,1]; NaN below the hard floor
    dsr: float                        # P(true SR > expected-max-of-n_trials); NaN below floor OR if trials_sr_std absent
    sr_star_deflated: float           # expected-max SR across n_trials (the DSR benchmark); NaN if trials_sr_std absent
    n_trials: int
    trials_sr_std: float | None       # supplied cross-trial Sharpe dispersion (None => DSR NaN)
    confidence: float
    sharpe_not_noise: bool            # DSR >= confidence AND n >= hard floor AND trials_sr_std supplied (NOT trade authorization)
    moments_used: bool                # False => fell back to normal-Sharpe (n < _MIN_N_MOMENTS)
    honest_flag: str

    def as_dict(self) -> dict:
        return asdict(self)


def sharpe_stats(returns) -> tuple[int, float, float, float]:
    """Return (n, per-observation sharpe, skew, Pearson kurtosis) from a return SERIES. Raises ValueError
    on < 2 finite points or zero variance. NEVER accepts a pre-annualized scalar — SR is computed here."""
    r = np.asarray([x for x in np.asarray(returns, dtype=float).ravel() if math.isfinite(x)], dtype=float)
    n = int(r.size)
    if n < 2:
        raise ValueError(f"need >= 2 finite returns, got {n}")
    sd = float(r.std(ddof=1))
    if not math.isfinite(sd) or sd <= 0.0:
        raise ValueError("zero / non-finite return variance — Sharpe undefined")
    sr = float(r.mean() / sd)
    sk = float(stats.skew(r, bias=False)) if n > 2 else 0.0
    ku = float(stats.kurtosis(r, fisher=False, bias=False)) if n > 3 else 3.0   # Pearson (normal = 3)
    if not math.isfinite(sk):
        sk = 0.0
    if not math.isfinite(ku) or ku <= 0.0:
        ku = 3.0
    return n, sr, sk, ku


def _sr_estimator_variance(sr: float, skew: float, kurtosis: float) -> float:
    """Bailey-LdP SR-estimator variance numerator: 1 − γ3·SR + ((γ4−1)/4)·SR², γ4 PEARSON (normal=3).
    Reduces to the classic Lo 1 + SR²/2 for normal returns (γ3=0, γ4=3 → (3−1)/4 = 0.5)."""
    return 1.0 - skew * sr + ((kurtosis - 1.0) / 4.0) * sr * sr


def probabilistic_sharpe_ratio(sr: float, n: int, skew: float, kurtosis: float, sr_benchmark: float = 0.0) -> float:
    """PSR — P(true per-observation SR > sr_benchmark). PSR = Φ((sr−sr*)·√(n−1)/√V), V the estimator
    variance above. sr / sr_benchmark are PER-OBSERVATION; kurtosis is Pearson. NaN on degenerate input."""
    if n < 2 or not all(math.isfinite(v) for v in (sr, skew, kurtosis, sr_benchmark)):
        return float("nan")
    var = _sr_estimator_variance(sr, skew, kurtosis)
    if var <= 0.0:
        return float("nan")
    z = (sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(var)
    return float(stats.norm.cdf(z))


def expected_max_sharpe(n_trials: int, trials_sr_std: float) -> float:
    """Expected MAXIMUM per-observation Sharpe across n_trials independent null strategies whose Sharpe
    estimates have dispersion trials_sr_std (Bailey & LdP 2014):
        E[max] ≈ trials_sr_std · [ (1−γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) ], γ = Euler–Mascheroni.
    n_trials ≤ 1 → 0.0 (no deflation)."""
    if n_trials <= 1 or not math.isfinite(trials_sr_std) or trials_sr_std <= 0.0:
        return 0.0
    n = float(n_trials)
    q1 = stats.norm.ppf(1.0 - 1.0 / n)
    q2 = stats.norm.ppf(1.0 - 1.0 / (n * math.e))
    if not (math.isfinite(q1) and math.isfinite(q2)):
        return 0.0
    return float(trials_sr_std * ((1.0 - _EULER) * q1 + _EULER * q2))


def deflated_sharpe(returns, n_trials: int, trials_sr_std: float | None = None,
                    confidence: float = 0.95) -> DSRResult:
    """Full evaluation: observed per-observation Sharpe → PSR (vs 0) and DSR (vs the expected-max of
    n_trials). See module docstring for the board-required contracts (frequency, hard n-floor, moment
    fallback, σ_trials required, exposed SE). `n_trials` is REQUIRED (count every variant tried)."""
    if not (isinstance(confidence, (int, float)) and 0.0 < float(confidence) < 1.0):
        raise ValueError(f"confidence must be a probability in (0,1), got {confidence!r}")  # a degenerate threshold must not manufacture a trivial pass (adversarial 2026-09-08)
    n, sr, sk, ku = sharpe_stats(returns)                     # raises on degenerate input — caller guards

    moments_used = n >= _MIN_N_MOMENTS
    _sk, _ku = (sk, ku) if moments_used else (0.0, 3.0)       # drop noisy γ3/γ4 below the reliability floor
    var = _sr_estimator_variance(sr, _sk, _ku)
    sr_se = math.sqrt(var / (n - 1)) if (var > 0.0 and n > 1) else float("nan")

    flags: list = []
    if not moments_used:
        flags.append(f"n<{_MIN_N_MOMENTS}: skew/kurtosis dropped (normal-Sharpe PSR — their SEs too large)")

    # Hard small-sample floor — below it, nothing is trustworthy (Thorp): NaN + sharpe_not_noise False.
    if n < _MIN_N_HARD:
        flags.append(f"n={n} < {_MIN_N_HARD} hard floor — SR/PSR/DSR not trustworthy; DO NOT act")
        return DSRResult(n, sr, sk, ku, sr_se, float("nan"), float("nan"), float("nan"),
                         int(n_trials), (float(trials_sr_std) if trials_sr_std is not None else None),
                         confidence, False, moments_used, "; ".join(flags))

    psr = probabilistic_sharpe_ratio(sr, n, _sk, _ku, sr_benchmark=0.0)

    # DSR requires a MEASURED cross-trial Sharpe dispersion. No fallback to the single-strategy SR SE
    # (a different statistic that silently over-states DSR — Gro/GAI/Thorp). Absent → DSR NaN, PSR only.
    if trials_sr_std is None or not math.isfinite(trials_sr_std) or trials_sr_std <= 0.0:
        # None / NaN / 0 / negative are ALL invalid dispersions — NaN is a realistic brick-2 output
        # (np.std of a single trial's Sharpe). Do NOT let any of them collapse sr_star to 0 and hand
        # back an UNdeflated PSR as a confident DSR (adversarial 2026-09-08). DSR NaN, PSR only.
        sr_star = float("nan")
        dsr = float("nan")
        flags.append("trials_sr_std missing/invalid (None/NaN/<=0) — DSR needs a measured POSITIVE cross-trial Sharpe dispersion; PSR only")
    else:
        sr_star = expected_max_sharpe(int(n_trials), float(trials_sr_std))
        dsr = probabilistic_sharpe_ratio(sr, n, _sk, _ku, sr_benchmark=sr_star)
        if int(n_trials) <= 1:
            flags.append("n_trials <= 1 — no multiple-testing deflation (DSR == PSR)")

    sharpe_not_noise = bool(math.isfinite(dsr) and dsr >= confidence)   # NaN dsr (no σ_trials / below floor) → False
    return DSRResult(n, sr, sk, ku, sr_se, psr, dsr, sr_star, int(n_trials),
                     (float(trials_sr_std) if trials_sr_std is not None else None),
                     confidence, sharpe_not_noise, moments_used, "; ".join(flags))


if __name__ == "__main__":   # tiny self-demo (not a test — see tests/test_deflated_sharpe.py)
    _rng = np.random.default_rng(7)
    _good = _rng.normal(0.08, 1.0, 260)          # small positive per-obs edge, 260 obs
    _res = deflated_sharpe(_good, n_trials=20, trials_sr_std=0.5)
    print(f"SR={_res.sharpe:.3f} PSR={_res.psr:.3f} DSR={_res.dsr:.3f} SR*={_res.sr_star_deflated:.3f} "
          f"SE={_res.sr_standard_error:.3f} not_noise={_res.sharpe_not_noise} flag='{_res.honest_flag}'")
